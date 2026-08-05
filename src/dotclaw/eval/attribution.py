"""失败归因：基于 Trace + EvalResult 找出最早决定性失败证据。

``FailureAttributor.attribute()`` 以固定普通规则扫描 Trace 的时间
/ sequence 顺序，首个命中的决定性证据为主因，之后只收集次要原因。
不读写文件、不调用 LLM、不改变 Gate 真值。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .attribution_rules import AttributionCategory
from .models import SCHEMA_VERSION

if TYPE_CHECKING:
    from ..trace.models import RunTrace, SpanKind, TraceSpan
    from .results import EvalResult, EvaluationFailureKind

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
    """置信度：HIGH（Trace+Result 双重证据）、MEDIUM（仅 Trace）、UNKNOWN（无充分证据）。"""
    decisive_span_id: str | None
    """决定性 Span 的唯一标识；无时为空。"""
    evidence: str
    """人类可读证据描述。"""
    secondary_causes: tuple[str, ...] = ()
    """不改变主因判定的次要原因类别列表。"""


# ---------------------------------------------------------------------------
# 置信度常量
# ---------------------------------------------------------------------------


_CONFIDENCE_HIGH: str = "HIGH"
_CONFIDENCE_MEDIUM: str = "MEDIUM"
_CONFIDENCE_UNKNOWN: str = "UNKNOWN"

# 不可归因的失败类别：属于评测基础设施，不伪装为 Agent 归因。
_INFRASTRUCTURE_KINDS: frozenset[str] = frozenset(
    {"fixture_configuration", "trace_reconstruction", "runtime"}
)


def _unk(
    evidence: str,
    *,
    secondary: tuple[str, ...] = (),
) -> AttributionResult:
    """构造 UNKNOWN 归因（无充分证据或基础设施错误）。"""
    return AttributionResult(
        schema_version=ATTRIBUTION_SCHEMA_VERSION,
        category=AttributionCategory.UNKNOWN,
        confidence=_CONFIDENCE_UNKNOWN,
        decisive_span_id=None,
        evidence=evidence,
        secondary_causes=secondary,
    )


# ---------------------------------------------------------------------------
# FailureAttributor
# ---------------------------------------------------------------------------


class FailureAttributor:
    """基于 Trace + EvalResult 的固定规则失败归因器。

    归因逻辑以有序纯函数列表实现；基础设施错误返回 UNKNOWN，
    不伪装为 Agent 归因。
    """

    def attribute(self, trace: RunTrace, result: EvalResult) -> AttributionResult:
        """对一次失败执行进行归因。

        若 ``result`` 未携带 ``trace``，仅能从 EvalResult 证据做有限判定。
        """
        # ── 基础设施错误 → 不可归因，立即返回 ──
        infra = _check_infrastructure(result)
        if infra is not None:
            return infra

        # ── 按顺序执行规则：首因优先 ──
        primary: AttributionResult | None = None
        secondaries: list[str] = []

        for rule in _RULES:
            hit = rule(trace, result)
            if hit is None:
                continue
            if primary is None:
                primary = hit
            else:
                # 主因已定，后续只收集次要原因
                secondaries.append(hit.category)
                secondaries.extend(hit.secondary_causes)

        if primary is None:
            return _unk("未找到可归因的证据")

        return AttributionResult(
            schema_version=primary.schema_version,
            category=primary.category,
            confidence=primary.confidence,
            decisive_span_id=primary.decisive_span_id,
            evidence=primary.evidence,
            secondary_causes=tuple(dict.fromkeys(secondaries)),  # 去重保序
        )


# ---------------------------------------------------------------------------
# 基础设施检查
# ---------------------------------------------------------------------------


def _check_infrastructure(result: EvalResult) -> AttributionResult | None:
    """基础设施失败不可归因，返回 UNKNOWN 并保留原分类。"""
    if result.failure_kind is not None:
        kind_val = result.failure_kind.value
        if kind_val in _INFRASTRUCTURE_KINDS:
            return _unk(
                f"基础设施错误（{kind_val}），不可归因为 Agent 行为"
                + (f"：{result.failure_detail}" if result.failure_detail else ""),
            )
    return None


# ---------------------------------------------------------------------------
# 顺序规则列表（按确定性由高到低排列）
# ---------------------------------------------------------------------------


def _ctx_build_failure(trace: RunTrace, result: EvalResult) -> AttributionResult | None:
    """上下文构建失败。"""
    from ..runtime.domain.facts import RunErrorCode
    err = trace.run.error
    if err is not None and err.code in (RunErrorCode.CONTEXT_BUDGET, RunErrorCode.TOKENIZER_UNAVAILABLE):
        return AttributionResult(
            schema_version=ATTRIBUTION_SCHEMA_VERSION,
            category=AttributionCategory.CONTEXT_BUILD_FAILURE,
            confidence=_CONFIDENCE_HIGH if _assertion_failed(result) else _CONFIDENCE_MEDIUM,
            decisive_span_id=None,
            evidence=f"上下文构建失败：{err.code.value} — {err.message}",
        )
    return None


def _ctx_budget_exceeded(trace: RunTrace, result: EvalResult) -> AttributionResult | None:
    """上下文超出 token 预算。"""
    from ..runtime.domain.facts import RunErrorCode
    err = trace.run.error
    if err is not None and err.code is RunErrorCode.CONTEXT_BUDGET:
        return AttributionResult(
            schema_version=ATTRIBUTION_SCHEMA_VERSION,
            category=AttributionCategory.CONTEXT_BUDGET_EXCEEDED,
            confidence=_CONFIDENCE_HIGH if _assertion_failed(result) else _CONFIDENCE_MEDIUM,
            decisive_span_id=None,
            evidence=f"上下文超出 token 预算：{err.message}",
        )
    return None


def _llm_unavailable(trace: RunTrace, result: EvalResult) -> AttributionResult | None:
    """LLM 服务不可用或调用失败。"""
    from ..runtime.domain.facts import RunErrorCode
    from ..trace.models import SpanKind, TraceSpanStatus

    err = trace.run.error
    if err is not None and err.code is RunErrorCode.LLM_FAILURE:
        return AttributionResult(
            schema_version=ATTRIBUTION_SCHEMA_VERSION,
            category=AttributionCategory.LLM_UNAVAILABLE,
            confidence=_CONFIDENCE_HIGH if _assertion_failed(result) else _CONFIDENCE_MEDIUM,
            decisive_span_id=None,
            evidence=f"LLM 服务不可用：{err.message}",
        )
    # 遍历 LLM Span 找 FAILED 状态
    for span in _llm_ordered(trace):
        if span.status is TraceSpanStatus.FAILED:
            return _from_span(
                trace, result, span,
                AttributionCategory.LLM_UNAVAILABLE,
                f"LLM 调用失败（span={span.span_id}）",
            )
    return None


def _llm_invalid_action(trace: RunTrace, result: EvalResult) -> AttributionResult | None:
    """LLM 产生无效动作——仅匹配 SPECIFICALLY LLM 相关的 TraceIssue，不匹配普通缺失消息。"""
    from ..trace.models import SpanKind, TraceIssueKind

    for span in _llm_ordered(trace):
        issues = _issues_for_span(trace, span.span_id)
        for issue in issues:
            # 只对真正表示 LLM 异常的 issue 类型触发
            if issue.kind in (TraceIssueKind.UNSUPPORTED_EVENT, TraceIssueKind.CONFLICTING_REFERENCE):
                return _from_span(
                    trace, result, span,
                    AttributionCategory.LLM_INVALID_ACTION,
                    f"LLM 产生无效动作（span={span.span_id}）：{issue.evidence}",
                )
    return None


def _wrong_tool_selected(trace: RunTrace, result: EvalResult) -> AttributionResult | None:
    """调用了错误的工具——来自 tool_sequence 断言失败。"""
    for ar in result.assertion_results:
        if not ar.passed and ar.expectation.kind == "tool_sequence":
            return AttributionResult(
                schema_version=ATTRIBUTION_SCHEMA_VERSION,
                category=AttributionCategory.WRONG_TOOL_SELECTED,
                confidence=_CONFIDENCE_HIGH,
                decisive_span_id=None,
                evidence=f"工具序列不符预期：{ar.evidence}",
            )
    return None


def _tool_arg_invalid(trace: RunTrace, result: EvalResult) -> AttributionResult | None:
    """工具参数不合法——来自 tool_arguments 断言失败。"""
    for ar in result.assertion_results:
        if not ar.passed and ar.expectation.kind == "tool_arguments":
            return AttributionResult(
                schema_version=ATTRIBUTION_SCHEMA_VERSION,
                category=AttributionCategory.TOOL_ARGUMENT_INVALID,
                confidence=_CONFIDENCE_HIGH,
                decisive_span_id=None,
                evidence=f"工具参数不符预期：{ar.evidence}",
            )
    return None


def _tool_exec_failed(trace: RunTrace, result: EvalResult) -> AttributionResult | None:
    """工具执行失败——寻找 FAILED 或 CANCELLED 状态的工具 Span。"""
    from ..trace.models import SpanKind, TraceSpanStatus

    for span in _tool_ordered(trace):
        if span.status in (TraceSpanStatus.FAILED, TraceSpanStatus.CANCELLED):
            return _from_span(
                trace, result, span,
                AttributionCategory.TOOL_EXECUTION_FAILED,
                f"工具执行失败（span={span.span_id}, status={span.status.value}）",
            )
    return None


def _policy_denied(trace: RunTrace, result: EvalResult) -> AttributionResult | None:
    """Policy 拒绝：trace.run.error 为 INVALID_STATE 且与策略相关。"""
    from ..runtime.domain.facts import RunErrorCode
    err = trace.run.error
    if err is not None and err.code is RunErrorCode.INVALID_STATE:
        return AttributionResult(
            schema_version=ATTRIBUTION_SCHEMA_VERSION,
            category=AttributionCategory.POLICY_DENIED,
            confidence=_CONFIDENCE_HIGH if _assertion_failed(result) else _CONFIDENCE_MEDIUM,
            decisive_span_id=None,
            evidence=f"Policy 拒绝：{err.message}",
        )
    return None


def _approval_rejected(trace: RunTrace, result: EvalResult) -> AttributionResult | None:
    """审批被拒绝。"""
    from ..trace.models import SpanKind

    for span in _approval_ordered(trace):
        approved = span.attributes.get("approved")
        if approved is False:
            return _from_span(
                trace, result, span,
                AttributionCategory.APPROVAL_REJECTED,
                f"审批被拒绝（span={span.span_id}）",
            )
    return None


def _delegation_failed(trace: RunTrace, result: EvalResult) -> AttributionResult | None:
    """委派失败。"""
    from ..trace.models import SpanKind, TraceSpanStatus

    for span in _delegation_ordered(trace):
        if span.status in (TraceSpanStatus.FAILED, TraceSpanStatus.CANCELLED):
            outcome = span.attributes.get("outcome", "unknown")
            return _from_span(
                trace, result, span,
                AttributionCategory.DELEGATION_FAILED,
                f"委派失败（span={span.span_id}, outcome={outcome}）",
            )
    return None


def _goal_not_completed(trace: RunTrace, result: EvalResult) -> AttributionResult | None:
    """运行结束但未达成目标。"""
    from ..runtime.domain.state import RunOutcome
    outcome = trace.run.state.outcome()
    if outcome is not None and outcome is not RunOutcome.COMPLETED:
        return AttributionResult(
            schema_version=ATTRIBUTION_SCHEMA_VERSION,
            category=AttributionCategory.GOAL_NOT_COMPLETED,
            confidence=_CONFIDENCE_HIGH if _assertion_failed(result) else _CONFIDENCE_MEDIUM,
            decisive_span_id=None,
            evidence=f"运行未达成目标：终态={outcome.value}",
        )
    return None


def _iteration_budget_exceeded(trace: RunTrace, result: EvalResult) -> AttributionResult | None:
    """超出迭代预算——来自 iteration_budget 断言失败。"""
    for ar in result.assertion_results:
        if not ar.passed and ar.expectation.kind == "iteration_budget":
            return AttributionResult(
                schema_version=ATTRIBUTION_SCHEMA_VERSION,
                category=AttributionCategory.ITERATION_BUDGET_EXCEEDED,
                confidence=_CONFIDENCE_HIGH,
                decisive_span_id=None,
                evidence=f"超出迭代预算：{ar.evidence}",
            )
    return None


def _token_regression(trace: RunTrace, result: EvalResult) -> AttributionResult | None:
    """Token 使用超过基线——来自 token_budget 断言失败。"""
    for ar in result.assertion_results:
        if not ar.passed and ar.expectation.kind == "token_budget":
            return AttributionResult(
                schema_version=ATTRIBUTION_SCHEMA_VERSION,
                category=AttributionCategory.TOKEN_REGRESSION,
                confidence=_CONFIDENCE_HIGH,
                decisive_span_id=None,
                evidence=f"Token 回归：{ar.evidence}",
            )
    return None


# ---------------------------------------------------------------------------
# 有序规则列表——按确定性由高到低排列
# ---------------------------------------------------------------------------


_RULES = (
    _ctx_build_failure,
    _ctx_budget_exceeded,
    _llm_unavailable,
    _wrong_tool_selected,
    _tool_arg_invalid,
    _tool_exec_failed,
    _policy_denied,
    _approval_rejected,
    _delegation_failed,
    _goal_not_completed,
    _iteration_budget_exceeded,
    _token_regression,
    _llm_invalid_action,  # 兜底：更具体的类别已排除
)


# ---------------------------------------------------------------------------
# 内部辅助
# ---------------------------------------------------------------------------


def _assertion_failed(result: EvalResult) -> bool:
    """是否有断言失败（排除通过和基础设施错误）。"""
    if result.failure_kind is None:
        return False
    return result.failure_kind.value == "assertion"


def _llm_ordered(trace: RunTrace):
    from ..eval.scorers._helpers import llm_spans
    return llm_spans(trace)


def _tool_ordered(trace: RunTrace):
    from ..eval.scorers._helpers import tool_spans
    return tool_spans(trace)


def _approval_ordered(trace: RunTrace):
    from ..eval.scorers._helpers import approval_spans
    return approval_spans(trace)


def _delegation_ordered(trace: RunTrace):
    from ..trace.models import SpanKind
    from ..eval.scorers._helpers import ordered_spans
    return ordered_spans(trace, SpanKind.DELEGATION)


def _issues_for_span(trace: RunTrace, span_id: str):
    return [issue for issue in trace.issues if issue.span_id == span_id]


def _from_span(
    trace: RunTrace,
    result: EvalResult,
    span,
    category: AttributionCategory,
    evidence: str,
) -> AttributionResult:
    """构造按 Span 归因的结果。"""
    return AttributionResult(
        schema_version=ATTRIBUTION_SCHEMA_VERSION,
        category=category,
        confidence=_CONFIDENCE_HIGH if _assertion_failed(result) else _CONFIDENCE_MEDIUM,
        decisive_span_id=span.span_id,
        evidence=evidence,
    )
