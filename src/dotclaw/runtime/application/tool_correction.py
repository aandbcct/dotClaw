"""工具重复失败策略及账本派生限制视图。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol

from ...common.tool_replay import ToolExecutionEvidence, ToolSideEffect
from ..domain.facts import ToolCall
from ..domain.tool_recovery import (
    ToolCallKind,
    ToolCallLedger,
    ToolCallState,
    ToolOutcomeOrigin,
    ToolOutcomeSnapshot,
    ToolOutcomeStatus,
)


@dataclass(frozen=True)
class ToolCorrectionContext:
    """从当前账本派生的单次调用限制视图。"""

    call: ToolCall
    consecutive_failures: int
    failure_limit: int
    blocked_tool_names: frozenset[str]
    rejected_call_keys: frozenset[str]
    rejected_operation_keys: frozenset[str]


@dataclass(frozen=True)
class ToolGuardDecision:
    """纯策略返回的工具准入决定。"""

    allowed: bool
    error_code: str | None = None
    message: str = ""


class ToolRepeatPolicy(Protocol):
    """只判断同工具和参数组合的连续执行失败。"""

    def evaluate(self, context: ToolCorrectionContext) -> ToolGuardDecision: ...


class ConsecutiveFailurePolicy:
    """达到冻结失败阈值后拒绝下一次匹配调用。"""

    def evaluate(self, context: ToolCorrectionContext) -> ToolGuardDecision:
        if context.consecutive_failures >= context.failure_limit:
            return ToolGuardDecision(False, "REPEAT_FAILURE_LIMIT", "该工具和参数组合已达到连续执行失败上限")
        return ToolGuardDecision(True)


def canonical_call_key(call: ToolCall) -> str:
    """生成仅规范对象键顺序的调用键，不持久化该键。"""
    return json.dumps(
        [call.name, call.arguments],
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )


def build_correction_context(
    ledger: ToolCallLedger,
    call: ToolCall,
    failure_limit: int,
) -> ToolCorrectionContext:
    """按账本顺序折叠失败计数、拒绝键和未知工具封禁。"""
    if not isinstance(failure_limit, int) or isinstance(failure_limit, bool) or failure_limit < 1:
        raise ValueError("failure_limit 必须是正整数")
    current_key = canonical_call_key(call)
    failures = 0
    blocked: set[str] = set()
    rejected_calls: set[str] = set()
    rejected_operations: set[str] = set()
    for record in ledger.calls:
        if record.approval_decision is False:
            rejected_calls.add(canonical_call_key(record.call))
            rejected_operations.update(record.approval_operation_keys)
        outcome = record.result
        if record.kind is ToolCallKind.ORDINARY and (
            record.state is ToolCallState.UNCERTAIN
            or (
                outcome is not None
                and (
                    outcome.status is ToolOutcomeStatus.UNCERTAIN
                    or (outcome.execution_evidence is ToolExecutionEvidence.UNKNOWN and record.side_effect is not ToolSideEffect.NONE)
                )
            )
        ):
            blocked.add(record.call.name)
        if record.kind is not ToolCallKind.ORDINARY or record.call.name != call.name:
            continue
        if outcome is not None and outcome.status is ToolOutcomeStatus.COMPLETED and canonical_call_key(record.call) == current_key:
            failures = 0
        elif (
            outcome is not None
            and outcome.status is ToolOutcomeStatus.FAILED
            and outcome.origin is ToolOutcomeOrigin.EXECUTION
            and canonical_call_key(record.call) == current_key
        ):
            failures += 1
    return ToolCorrectionContext(
        call=call,
        consecutive_failures=failures,
        failure_limit=failure_limit,
        blocked_tool_names=frozenset(blocked),
        rejected_call_keys=frozenset(rejected_calls),
        rejected_operation_keys=frozenset(rejected_operations),
    )


def format_tool_feedback(call: ToolCall, outcome: ToolOutcomeSnapshot) -> str:
    """将非成功结果格式化为稳定反馈 JSON；成功正文保持原样。"""
    if outcome.status is ToolOutcomeStatus.COMPLETED:
        return outcome.output
    error_code = outcome.tool_error_code or (None if outcome.error is None else outcome.error.code.value)
    constraint_by_code = {
        "APPROVAL_DENIED": "approval_denied",
        "REPEAT_FAILURE_LIMIT": "repeat_failure_limit",
        "TOOL_BLOCKED_UNCERTAIN": "tool_blocked_uncertain",
        "TOOL_OUTCOME_UNCERTAIN": "tool_blocked_uncertain",
        "tool_outcome_uncertain": "tool_blocked_uncertain",
    }
    constraint = constraint_by_code.get(error_code, "none")
    payload = {
        "status": outcome.status.value,
        "tool_name": call.name,
        "error_code": error_code,
        "error_type": outcome.tool_error_type,
        "message": outcome.output or ("" if outcome.error is None else outcome.error.message),
        "execution_evidence": None if outcome.execution_evidence is None else outcome.execution_evidence.value,
        "effect_certainty": outcome.effect_certainty.value,
        "constraint": constraint,
    }
    if constraint == "tool_blocked_uncertain":
        payload["blocked_tool_name"] = call.name
        original_message = payload["message"]
        restriction = f"工具 {call.name} 的既有调用结果未知；该工具暂被限制，其他工具仍可用。"
        payload["message"] = f"{original_message} {restriction}" if original_message else restriction
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
