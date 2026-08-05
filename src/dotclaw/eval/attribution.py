"""失败归因：基于 Trace + EvalResult 找出最早决定性失败证据。

``FailureAttributor.attribute()`` 收集全部规则命中，再按 Trace 时间
/ sequence 顺序选取最早者为主因，其余为次要原因。
不读写文件、不调用 LLM、不改变 Gate 真值。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .attribution_rules import AttributionCategory
from .models import SCHEMA_VERSION

if TYPE_CHECKING:
    from ..trace.models import RunTrace, TraceSpan
    from .results import EvalResult

ATTRIBUTION_SCHEMA_VERSION: str = SCHEMA_VERSION


@dataclass(frozen=True)
class AttributionResult:
    """一次失败归因结果。

    仅记录最早决定性证据；次要原因不改变主因判定。
    """

    schema_version: str
    """归因结果 schema 版本。"""
    category: str
    """归因类别（AttributionCategory 枚举值）。"""
    confidence: str
    """HIGH / MEDIUM / UNKNOWN。"""
    decisive_span_id: str | None
    """决定性 Span 的唯一标识。"""
    evidence: str
    """人类可读证据。"""
    secondary_causes: tuple[str, ...] = ()
    """不改变主因判定的次要原因类别。"""


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

_CONFIDENCE_HIGH: str = "HIGH"
_CONFIDENCE_MEDIUM: str = "MEDIUM"
_CONFIDENCE_UNKNOWN: str = "UNKNOWN"

_INFRASTRUCTURE_KINDS: frozenset[str] = frozenset(
    {"fixture_configuration", "trace_reconstruction", "runtime"}
)


def _unk(evidence: str) -> AttributionResult:
    return AttributionResult(
        schema_version=ATTRIBUTION_SCHEMA_VERSION,
        category=AttributionCategory.UNKNOWN,
        confidence=_CONFIDENCE_UNKNOWN,
        decisive_span_id=None,
        evidence=evidence,
    )


# ---------------------------------------------------------------------------
# FailureAttributor
# ---------------------------------------------------------------------------


class FailureAttributor:
    """基于 Trace + EvalResult 的固定规则失败归因器。

    收集全部规则命中，按 Trace 时间 / sequence 顺序选取最早的为
    主因（非固定规则列表顺序）。
    """

    def attribute(self, trace: RunTrace, result: EvalResult) -> AttributionResult:
        """对一次失败执行进行归因。

        先排除基础设施错误；然后收集全部规则命中，按 Span 发生的
        时间 / sequence 排序，取最早者为主因。
        """
        infra = _check_infrastructure(result)
        if infra is not None:
            return infra

        # 收集全部命中
        candidates: list[AttributionResult] = []
        for rule in _RULES:
            hit = rule(trace, result)
            if hit is not None:
                candidates.append(hit)

        if not candidates:
            return _unk("未找到可归因的证据")

        # 按 Span 时序排序：有 span 的按 start_event_sequence，无 span 的排最后
        candidates.sort(key=lambda c: _span_seq(trace, c))

        primary = candidates[0]
        secondaries = [c.category for c in candidates[1:]]

        return AttributionResult(
            schema_version=primary.schema_version,
            category=primary.category,
            confidence=primary.confidence,
            decisive_span_id=primary.decisive_span_id,
            evidence=primary.evidence,
            secondary_causes=tuple(dict.fromkeys(secondaries)),
        )


# ---------------------------------------------------------------------------
# 基础设施
# ---------------------------------------------------------------------------


def _check_infrastructure(result: EvalResult) -> AttributionResult | None:
    if result.failure_kind is not None:
        kind_val = result.failure_kind.value
        if kind_val in _INFRASTRUCTURE_KINDS:
            return _unk(
                f"基础设施错误（{kind_val}），不可归因为 Agent 行为"
                + (f"：{result.failure_detail}" if result.failure_detail else ""),
            )
    return None


# ---------------------------------------------------------------------------
# Context 规则
# ---------------------------------------------------------------------------


def _ctx_build_failure(trace: RunTrace, result: EvalResult) -> AttributionResult | None:
    """上下文构建失败——仅 TOKENIZER_UNAVAILABLE 等无法构建的情况，不含预算超限。"""
    from ..runtime.domain.facts import RunErrorCode
    err = trace.run.error
    if err is not None and err.code is RunErrorCode.TOKENIZER_UNAVAILABLE:
        return _no_span(
            result, AttributionCategory.CONTEXT_BUILD_FAILURE,
            f"上下文构建失败：{err.code.value} — {err.message}",
        )
    return None


def _ctx_budget_exceeded(trace: RunTrace, result: EvalResult) -> AttributionResult | None:
    """上下文超出 token 预算——CONTEXT_BUDGET 错误码。"""
    from ..runtime.domain.facts import RunErrorCode
    err = trace.run.error
    if err is not None and err.code is RunErrorCode.CONTEXT_BUDGET:
        return _no_span(
            result, AttributionCategory.CONTEXT_BUDGET_EXCEEDED,
            f"上下文超出 token 预算：{err.message}",
        )
    return None


def _ctx_information_lost(trace: RunTrace, result: EvalResult) -> AttributionResult | None:
    """上下文信息丢失——存在历史压缩记录。"""
    if trace.run.staged_history_compressions:
        return _no_span(
            result, AttributionCategory.CONTEXT_INFORMATION_LOST,
            f"上下文经历 {len(trace.run.staged_history_compressions)} 次压缩，信息可能丢失",
        )
    # 备选信号：INCOMPLETE 状态的上下文相关 Span
    from ..trace.models import TraceSpanStatus
    for issue in trace.issues:
        if issue.kind.value == "missing_context_version":
            return _no_span(
                result, AttributionCategory.CONTEXT_INFORMATION_LOST,
                f"上下文信息丢失：{issue.evidence}",
            )
    return None


# ---------------------------------------------------------------------------
# LLM 规则
# ---------------------------------------------------------------------------


def _llm_unavailable(trace: RunTrace, result: EvalResult) -> AttributionResult | None:
    """LLM 服务不可用。"""
    from ..runtime.domain.facts import RunErrorCode
    from ..trace.models import TraceSpanStatus

    err = trace.run.error
    if err is not None and err.code is RunErrorCode.LLM_FAILURE:
        return _no_span(
            result, AttributionCategory.LLM_UNAVAILABLE,
            f"LLM 服务不可用：{err.message}",
        )
    for span in _llm_ordered(trace):
        if span.status is TraceSpanStatus.FAILED:
            return _span_attr(
                result, span, AttributionCategory.LLM_UNAVAILABLE,
                f"LLM 调用失败（span={span.span_id}）",
            )
    return None


def _llm_invalid_action(trace: RunTrace, result: EvalResult) -> AttributionResult | None:
    """LLM 产生无效动作——匹配 LLM Span 上的语义冲突 Issue。"""
    from ..trace.models import TraceIssueKind

    for span in _llm_ordered(trace):
        for issue in _issues_for_span(trace, span.span_id):
            if issue.kind in (TraceIssueKind.UNSUPPORTED_EVENT, TraceIssueKind.CONFLICTING_REFERENCE):
                return _span_attr(
                    result, span, AttributionCategory.LLM_INVALID_ACTION,
                    f"LLM 产生无效动作（span={span.span_id}）：{issue.evidence}",
                )
    return None


# ---------------------------------------------------------------------------
# Tool 规则
# ---------------------------------------------------------------------------


def _wrong_tool_selected(trace: RunTrace, result: EvalResult) -> AttributionResult | None:
    for ar in result.assertion_results:
        if not ar.passed and ar.expectation.kind == "tool_sequence":
            return _no_span(
                result, AttributionCategory.WRONG_TOOL_SELECTED,
                f"工具序列不符预期：{ar.evidence}",
            )
    return None


def _tool_arg_invalid(trace: RunTrace, result: EvalResult) -> AttributionResult | None:
    """工具参数不合法——匹配 PR4 的 tool_argument 断言（非 tool_arguments）。"""
    for ar in result.assertion_results:
        if not ar.passed and ar.expectation.kind == "tool_argument":
            return _no_span(
                result, AttributionCategory.TOOL_ARGUMENT_INVALID,
                f"工具参数不符预期：{ar.evidence}",
            )
    return None


def _tool_exec_failed(trace: RunTrace, result: EvalResult) -> AttributionResult | None:
    from ..trace.models import TraceSpanStatus
    for span in _tool_ordered(trace):
        if span.status in (TraceSpanStatus.FAILED, TraceSpanStatus.CANCELLED):
            return _span_attr(
                result, span, AttributionCategory.TOOL_EXECUTION_FAILED,
                f"工具执行失败（span={span.span_id}, status={span.status.value}）",
            )
    return None


# ---------------------------------------------------------------------------
# Policy / Approval 规则
# ---------------------------------------------------------------------------


def _policy_denied(trace: RunTrace, result: EvalResult) -> AttributionResult | None:
    from ..runtime.domain.facts import RunErrorCode
    err = trace.run.error
    if err is not None and err.code is RunErrorCode.INVALID_STATE:
        return _no_span(
            result, AttributionCategory.POLICY_DENIED,
            f"Policy 拒绝：{err.message}",
        )
    return None


def _unnecessary_approval(trace: RunTrace, result: EvalResult) -> AttributionResult | None:
    """不必要的审批——审批被触发但关联的工具实际是 COMPLETED 而非 APPROVAL_REQUIRED。"""
    from ..trace.models import TraceSpanStatus

    for appr_span in _approval_ordered(trace):
        appr_call_id = appr_span.attributes.get("call_id")
        if appr_call_id is None:
            continue
        # 查找关联的工具 Span：若审批之前工具已以 COMPLETED 结束，审批是多余的
        for tool_span in _tool_ordered(trace):
            tc = tool_span.attributes.get("call_id")
            if tc == appr_call_id and tool_span.status is TraceSpanStatus.COMPLETED:
                # 工具已成功但仍有审批等待 → 审批不必要
                return _span_attr(
                    result, appr_span, AttributionCategory.UNNECESSARY_APPROVAL,
                    f"不必要审批（span={appr_span.span_id}）：关联工具 {appr_call_id} 已完成",
                )
    return None


def _approval_rejected(trace: RunTrace, result: EvalResult) -> AttributionResult | None:
    for span in _approval_ordered(trace):
        if span.attributes.get("approved") is False:
            return _span_attr(
                result, span, AttributionCategory.APPROVAL_REJECTED,
                f"审批被拒绝（span={span.span_id}）",
            )
    return None


# ---------------------------------------------------------------------------
# Delegation / Goal / Budget 规则
# ---------------------------------------------------------------------------


def _delegation_failed(trace: RunTrace, result: EvalResult) -> AttributionResult | None:
    from ..trace.models import TraceSpanStatus
    for span in _delegation_ordered(trace):
        if span.status in (TraceSpanStatus.FAILED, TraceSpanStatus.CANCELLED):
            outcome = span.attributes.get("outcome", "unknown")
            return _span_attr(
                result, span, AttributionCategory.DELEGATION_FAILED,
                f"委派失败（span={span.span_id}, outcome={outcome}）",
            )
    return None


def _goal_not_completed(trace: RunTrace, result: EvalResult) -> AttributionResult | None:
    from ..runtime.domain.state import RunOutcome
    outcome = trace.run.state.outcome()
    if outcome is not None and outcome is not RunOutcome.COMPLETED:
        return _no_span(
            result, AttributionCategory.GOAL_NOT_COMPLETED,
            f"运行未达成目标：终态={outcome.value}",
        )
    return None


def _iteration_budget_exceeded(trace: RunTrace, result: EvalResult) -> AttributionResult | None:
    for ar in result.assertion_results:
        if not ar.passed and ar.expectation.kind == "iteration_budget":
            return _no_span(
                result, AttributionCategory.ITERATION_BUDGET_EXCEEDED,
                f"超出迭代预算：{ar.evidence}",
            )
    return None


def _token_regression(trace: RunTrace, result: EvalResult) -> AttributionResult | None:
    for ar in result.assertion_results:
        if not ar.passed and ar.expectation.kind == "token_budget":
            return _no_span(
                result, AttributionCategory.TOKEN_REGRESSION,
                f"Token 回归：{ar.evidence}",
            )
    return None


# ---------------------------------------------------------------------------
# 规则列表（所有规则平级收集，不按此顺序定主因）
# ---------------------------------------------------------------------------


_RULES = (
    _ctx_build_failure,
    _ctx_budget_exceeded,
    _ctx_information_lost,
    _llm_unavailable,
    _llm_invalid_action,
    _wrong_tool_selected,
    _tool_arg_invalid,
    _tool_exec_failed,
    _policy_denied,
    _unnecessary_approval,
    _approval_rejected,
    _delegation_failed,
    _goal_not_completed,
    _iteration_budget_exceeded,
    _token_regression,
)


# ---------------------------------------------------------------------------
# 时序排序辅助
# ---------------------------------------------------------------------------


def _span_seq(trace: RunTrace, attr: AttributionResult) -> int:
    """返回归因结果对应 Span 的时序键（越小越早）。

    无 Span 的归因（如 RunError）没有精确时间点，赋予一个大数排到最后，
    让有时序 Span 的结果优先被选为主因。
    """
    if attr.decisive_span_id is None:
        return 999999
    span = _find_span(trace, attr.decisive_span_id)
    if span is not None and span.start_event_sequence is not None:
        return span.start_event_sequence
    return 999999


def _find_span(trace: RunTrace, span_id: str):
    for s in trace.spans:
        if s.span_id == span_id:
            return s
    return None


# ---------------------------------------------------------------------------
# 内部构造辅助
# ---------------------------------------------------------------------------


def _no_span(
    result: EvalResult,
    category: AttributionCategory,
    evidence: str,
) -> AttributionResult:
    return AttributionResult(
        schema_version=ATTRIBUTION_SCHEMA_VERSION,
        category=category,
        confidence=_CONFIDENCE_HIGH if _assertion_failed(result) else _CONFIDENCE_MEDIUM,
        decisive_span_id=None,
        evidence=evidence,
    )


def _span_attr(
    result: EvalResult,
    span,
    category: AttributionCategory,
    evidence: str,
) -> AttributionResult:
    return AttributionResult(
        schema_version=ATTRIBUTION_SCHEMA_VERSION,
        category=category,
        confidence=_CONFIDENCE_HIGH if _assertion_failed(result) else _CONFIDENCE_MEDIUM,
        decisive_span_id=span.span_id,
        evidence=evidence,
    )


def _assertion_failed(result: EvalResult) -> bool:
    if result.failure_kind is None:
        return False
    return result.failure_kind.value == "assertion"


# ---------------------------------------------------------------------------
# Span 遍历
# ---------------------------------------------------------------------------


def _llm_ordered(trace):
    from ..eval.scorers._helpers import llm_spans
    return llm_spans(trace)


def _tool_ordered(trace):
    from ..eval.scorers._helpers import tool_spans
    return tool_spans(trace)


def _approval_ordered(trace):
    from ..eval.scorers._helpers import approval_spans
    return approval_spans(trace)


def _delegation_ordered(trace):
    from ..trace.models import SpanKind
    from ..eval.scorers._helpers import ordered_spans
    return ordered_spans(trace, SpanKind.DELEGATION)


def _issues_for_span(trace, span_id: str):
    return [issue for issue in trace.issues if issue.span_id == span_id]
