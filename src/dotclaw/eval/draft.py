"""EvalCaseDraft 模型：将终态 RunTrace 转换为可人工审阅的 EvalCase 草案。

Draft 与 Case 各自持有独立的 schema 版本；Draft 包装一个候选 ``EvalCase`` 载荷，
并记录来源 Trace 的标识与哈希、是否需要人工审阅、以及确认后的 Case 标识。本模块
不负责转换（见 ``trace_to_eval_case_draft``）也不负责存储（见 ``dataset``）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Mapping

from ..runtime.application.dto import ConversationMessage, ToolResultStatus
from ..runtime.domain.facts import JSONMap, MessageRole, RunMessage, RunMessageKind
from ..runtime.domain.state import RunOutcome
from ..trace.models import RunTrace, SpanKind, TraceSpanStatus
from .models import (
    EVAL_SCHEMA_VERSION,
    ApprovalFixture,
    ContextFixture,
    ConversationFixture,
    DelegationFixture,
    EvalCase,
    EvalCaseValidationError,
    Expectation,
    LLMFixture,
    LLMResponseFixture,
    ToolFixture,
    _optional_str,
    _require_map,
    _require_str,
)
from .scorers._helpers import message_by_id, ordered_spans, run_outcome

DRAFT_SCHEMA_VERSION: str = "1.0"
"""当前支持的 Draft schema 版本；读取到其他版本必须明确失败。"""


@dataclass(frozen=True, kw_only=True)
class EvalCaseDraft:
    """RunTrace 经人工审阅后落为 Case 的中间草案。

    草案只承载一份"候选 Case"载荷，不保证其可直接执行；``requires_review`` 标记
    表示该草案载荷存在需要人工处置的内容（如疑似敏感字段），确认前必须由 Channel
    经 ``save_reviewed_draft`` 显式清除。
    """

    draft_id: str
    source_run_id: str
    source_record_hash: str
    source_trace_schema_version: str
    case: EvalCase
    """候选 Case 载荷；其 schema_version 必须等于当前支持的 Case 版本。"""
    requires_review: bool = False
    confirmed_case_id: str | None = None
    """确认落库后回写的 Case 标识；非空表示本草案已完成确认。"""
    schema_version: str = DRAFT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        """校验 schema 版本、来源标识与全局唯一性约束。"""
        if self.schema_version != DRAFT_SCHEMA_VERSION:
            raise EvalCaseValidationError(
                f"不支持的 draft schema 版本 {self.schema_version!r}，当前仅支持 {DRAFT_SCHEMA_VERSION!r}"
            )
        if not self.draft_id:
            raise EvalCaseValidationError("draft_id 不能为空")
        if not self.source_run_id:
            raise EvalCaseValidationError("source_run_id 不能为空")
        if not self.source_record_hash:
            raise EvalCaseValidationError("source_record_hash 不能为空")
        if not self.source_trace_schema_version:
            raise EvalCaseValidationError("source_trace_schema_version 不能为空")
        if self.confirmed_case_id is not None and self.confirmed_case_id == "":
            raise EvalCaseValidationError("confirmed_case_id 不能为空字符串")

    def to_dict(self) -> JSONMap:
        """序列化为 JSON 兼容字典。"""
        return {
            "draft_id": self.draft_id,
            "schema_version": self.schema_version,
            "source_run_id": self.source_run_id,
            "source_record_hash": self.source_record_hash,
            "source_trace_schema_version": self.source_trace_schema_version,
            "case": self.case.to_dict(),
            "requires_review": self.requires_review,
            "confirmed_case_id": self.confirmed_case_id,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> EvalCaseDraft:
        """从 JSON 字典严格反序列化；版本或候选 Case 非法立即失败。"""
        label: str = "draft"
        schema_version: str = _require_str(data, "schema_version", label, allow_empty=False)
        if schema_version != DRAFT_SCHEMA_VERSION:
            raise EvalCaseValidationError(
                f"不支持的 draft schema 版本 {schema_version!r}，当前仅支持 {DRAFT_SCHEMA_VERSION!r}"
            )
        case: EvalCase = EvalCase.from_dict(
            _require_map(data.get("case"), f"{label}.case")
        )
        confirmed: str | None = _optional_str(data, "confirmed_case_id", label)
        return cls(
            draft_id=_require_str(data, "draft_id", label, allow_empty=False),
            schema_version=schema_version,
            source_run_id=_require_str(data, "source_run_id", label, allow_empty=False),
            source_record_hash=_require_str(data, "source_record_hash", label, allow_empty=False),
            source_trace_schema_version=_require_str(
                data, "source_trace_schema_version", label, allow_empty=False
            ),
            case=case,
            requires_review=bool(data.get("requires_review", False)),
            confirmed_case_id=confirmed,
        )


# ---------------------------------------------------------------------------
# RunTrace → EvalCaseDraft 转换
# ---------------------------------------------------------------------------
# 以下转换是单向、只读的：只消费 RunTrace 的不可变事实，不接触 Runtime 也不写文件。
# 部分（语义不完整）Trace 一律拒绝；终态 Trace 提取 input / 冻结 Policy / Context /
# Conversation / LLM / Tool / Approval / Delegation Fixture，以及基础 Expectation 与
# Token / 调用次数基线。缺失可复现细节（如 Context 明文、会话历史）以冻结空载体表达，
# 交由人工审阅阶段补全，不臆测替代内容。


def _conversation_from_run_message(message: RunMessage, created_at: str) -> ConversationMessage:
    """把运行消息投影为会话消息（运行消息无 created_at，使用给定值冻结）。"""
    return ConversationMessage(
        message_id=message.message_id,
        role=message.role,
        content=message.content,
        created_at=created_at,
    )


def _extract_input(trace: RunTrace) -> ConversationMessage:
    """提取入口用户输入消息；缺失时退而求其次取首条用户角色输入消息。"""
    run = trace.run
    message: RunMessage | None = None
    if run.input_message_id:
        message = message_by_id(trace, run.input_message_id)
    if message is None:
        for candidate in trace.messages:
            if candidate.role is MessageRole.USER and candidate.kind is RunMessageKind.USER_INPUT:
                message = candidate
                break
    if message is None:
        raise EvalCaseValidationError(f"Trace（run={run.run_id}）缺少可提取的用户输入消息")
    return _conversation_from_run_message(message, run.started_at or "")


def _tool_status_to_result(status: TraceSpanStatus) -> ToolResultStatus:
    """将追踪 Span 状态投影为工具结果类别；取消/不完整统一视为失败。"""
    if status is TraceSpanStatus.COMPLETED:
        return ToolResultStatus.COMPLETED
    if status is TraceSpanStatus.FAILED:
        return ToolResultStatus.FAILED
    if status is TraceSpanStatus.WAITING:
        return ToolResultStatus.APPROVAL_REQUIRED
    return ToolResultStatus.FAILED


def _tool_key_arguments(trace: RunTrace, span: object) -> dict[str, object]:
    """从工具源响应消息中按 call_id 取出工具调用关键参数。"""
    call_id = span.attributes.get("call_id") if hasattr(span, "attributes") else None  # type: ignore[attr-defined]
    source_id = span.message_ids[0] if getattr(span, "message_ids", ()) else None  # type: ignore[attr-defined]
    if not call_id or not source_id:
        return {}
    source = message_by_id(trace, source_id)
    if source is None:
        return {}
    for tool_call in source.tool_calls:
        if tool_call.call_id == call_id:
            return dict(tool_call.arguments)
    return {}


def _tool_output(trace: RunTrace, span: object) -> str:
    """读取工具结果正文。

    工具 Span 通常挂载「源响应消息 + 结果消息」两条；但经审批闭合的工具调用由
    ``APPROVAL_RESOLVED`` 结束 Span，重放产生的 ``TOOL_COMPLETED`` 被组装期视为
    良性重复而跳过，结果消息不会挂到 Span 上。此时按 ``call_id`` 回查工具结果消息，
    保证冻结的工具输出不因审批而丢失。
    """
    message_ids = getattr(span, "message_ids", ())  # type: ignore[attr-defined]
    if len(message_ids) >= 2:
        result = message_by_id(trace, message_ids[-1])
        if result is not None:
            return result.content
    call_id = span.attributes.get("call_id") if hasattr(span, "attributes") else None  # type: ignore[attr-defined]
    if not call_id:
        return ""
    for message in trace.messages:
        if message.kind is RunMessageKind.TOOL_RESULT and message.tool_call_id == call_id:
            return message.content
    return ""


def _delegation_output(trace: RunTrace, span: object) -> str:
    """读取委派结果正文；委派 Span 只挂载一条结果消息。"""
    message_ids = getattr(span, "message_ids", ())  # type: ignore[attr-defined]
    if not message_ids:
        return ""
    result = message_by_id(trace, message_ids[-1])
    return result.content if result is not None else ""


def _linked_approval_id(trace: RunTrace, span: object) -> str | None:
    """按 call_id 找到关联的审批标识（仅待审批工具需要）。"""
    call_id = span.attributes.get("call_id") if hasattr(span, "attributes") else None  # type: ignore[attr-defined]
    if not call_id:
        return None
    for approval_span in ordered_spans(trace, SpanKind.APPROVAL):
        if approval_span.attributes.get("call_id") == call_id:
            return approval_span.attributes.get("approval_id")
    return None


def _build_llm_fixture(trace: RunTrace) -> LLMFixture:
    """按有序 LLM Span 提取脚本化响应（content + tool_calls）。"""
    responses: list[LLMResponseFixture] = []
    for span in ordered_spans(trace, SpanKind.LLM):
        if not span.message_ids:
            continue
        message = message_by_id(trace, span.message_ids[0])
        if message is None:
            continue
        responses.append(
            LLMResponseFixture(
                message_id=message.message_id,
                content=message.content,
                tool_calls=tuple(message.tool_calls),
            )
        )
    return LLMFixture(fixture_id="llm-1", responses=tuple(responses))


def _build_tool_fixtures(trace: RunTrace) -> tuple[ToolFixture, ...]:
    """按有序工具 Span 提取冻结工具结果与匹配条件。"""
    fixtures: list[ToolFixture] = []
    for index, span in enumerate(ordered_spans(trace, SpanKind.TOOL), start=1):
        status = _tool_status_to_result(span.status)
        call_id = span.attributes.get("call_id")
        approval_id = (
            _linked_approval_id(trace, span) if status is ToolResultStatus.APPROVAL_REQUIRED else None
        )
        error_message = "工具调用已取消" if span.status is TraceSpanStatus.CANCELLED else ""
        fixtures.append(
            ToolFixture(
                fixture_id=f"tool-{call_id}" if call_id else f"tool-{index}",
                tool_name=span.attributes.get("tool_name") or "",
                key_arguments=_tool_key_arguments(trace, span),
                status=status,
                output=_tool_output(trace, span),
                approval_id=approval_id,
                error_message=error_message,
            )
        )
    return tuple(fixtures)


def _build_approval_fixtures(trace: RunTrace) -> tuple[ApprovalFixture, ...]:
    """按有序审批 Span 提取冻结决议。"""
    fixtures: list[ApprovalFixture] = []
    for index, span in enumerate(ordered_spans(trace, SpanKind.APPROVAL), start=1):
        approval_id = span.attributes.get("approval_id")
        fixtures.append(
            ApprovalFixture(
                fixture_id=f"approval-{approval_id}" if approval_id else f"approval-{index}",
                approved=bool(span.attributes.get("approved", False)),
                approval_id=approval_id,
            )
        )
    return tuple(fixtures)


def _build_delegation_fixtures(trace: RunTrace) -> tuple[DelegationFixture, ...]:
    """按有序委派 Span 提取冻结受理与结果。"""
    fixtures: list[DelegationFixture] = []
    for index, span in enumerate(ordered_spans(trace, SpanKind.DELEGATION), start=1):
        child_run_id = span.attributes.get("child_run_id")
        outcome_raw = span.attributes.get("outcome") or None
        outcome = RunOutcome(outcome_raw) if outcome_raw else None
        fixtures.append(
            DelegationFixture(
                fixture_id=f"delegation-{child_run_id}" if child_run_id else f"delegation-{index}",
                target_agent_id=span.attributes.get("target_agent_id") or "",
                child_run_id=child_run_id or "",
                task_id=span.attributes.get("task_id") or "",
                target_session_id=span.attributes.get("target_session_id") or "",
                outcome=outcome,
                output=_delegation_output(trace, span),
            )
        )
    return tuple(fixtures)


def _build_context_fixtures(trace: RunTrace) -> tuple[ContextFixture, ...]:
    """按上下文版本提取冻结上下文载体；明文不可复现，固定为空载体 + 版本标识。"""
    return tuple(
        ContextFixture(fixture_id=f"ctx-{version.version}", messages=(), tools=(), estimated_tokens=1)
        for version in trace.context_versions
    )


def _stable_source_view(trace: RunTrace) -> str:
    """渲染稳定的来源追踪视图。

    剔除 ``source.assembled_at``：它是组装时刻而非权威事实，保留会让同一份 Trace 在不同
    时刻转换出不同草案，破坏"同一 Trace → 同一 Draft"的可复现性。默认不导出正文。
    """
    payload = trace.to_dict(include_content=False)
    source = payload.get("source")
    if isinstance(source, dict):
        payload["source"] = {
            key: value for key, value in source.items() if key != "assembled_at"
        }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _build_base_expectations(
    trace: RunTrace, tool_fixtures: tuple[ToolFixture, ...]
) -> tuple[Expectation, ...]:
    """派生基础断言与 Token / 调用次数基线。"""
    expectations: list[Expectation] = []
    outcome = run_outcome(trace)
    if outcome is not None:
        expectations.append(Expectation(kind="run_status", target="run", expected=outcome.value))
    statistics = trace.run.statistics
    expectations.append(
        Expectation(
            kind="token_budget",
            target="run",
            expected={
                "tokens_in": statistics.tokens_in,
                "tokens_out": statistics.tokens_out,
                "tool_call_count": statistics.tool_call_count,
                "llm_call_count": statistics.llm_call_count,
            },
        )
    )
    expectations.append(
        Expectation(kind="iteration_budget", target="policy", expected=trace.run.policy.max_iterations)
    )
    tool_names = [fixture.tool_name for fixture in tool_fixtures]
    if tool_names:
        expectations.append(Expectation(kind="tool_sequence", target="run", expected=tool_names))
    return tuple(expectations)


def trace_to_eval_case_draft(
    trace: RunTrace,
    *,
    case_id: str | None = None,
    name: str = "",
) -> EvalCaseDraft:
    """将终态完整 ``RunTrace`` 转换为可人工审阅的 ``EvalCaseDraft``。

    仅接受 ``is_partial=False`` 的 Trace；部分 Trace 立即抛 ``EvalCaseValidationError``。
    转换结果包装一份候选 ``EvalCase``，其 ``source_trace`` 记录脱敏后的结构化追踪视图，
    供人工审阅时还原来源。
    """
    if trace.is_partial:
        raise EvalCaseValidationError(
            f"部分 Trace（run={trace.run.run_id}）不能转换为 Draft，请先修复运行完整性"
        )
    run = trace.run
    tool_fixtures = _build_tool_fixtures(trace)
    case = EvalCase(
        case_id=case_id or f"case-{run.run_id}",
        agent_id=run.agent_id,
        name=name or f"trace-{run.run_id}",
        input=_extract_input(trace),
        conversation_fixture=ConversationFixture(session_id=run.session_id, messages=(), version=0),
        policy_fixture=run.policy,
        context_fixtures=_build_context_fixtures(trace),
        llm_fixture=_build_llm_fixture(trace),
        tool_fixtures=tool_fixtures,
        approval_fixtures=_build_approval_fixtures(trace),
        delegation_fixtures=_build_delegation_fixtures(trace),
        expectations=_build_base_expectations(trace, tool_fixtures),
        source_trace=_stable_source_view(trace),
    )
    return EvalCaseDraft(
        draft_id=f"draft-{run.run_id}",
        source_run_id=run.run_id,
        source_record_hash=trace.source.record_hash,
        source_trace_schema_version=trace.schema_version,
        case=case,
    )
