"""失败归因的固定类别与有序纯函数规则。

每条规则是一个无副作用的纯函数：``(trace, result) -> AttributionResult | None``。
规则按本模块定义的顺序扫描 Trace（时间 / sequence 优先），首个有充分证据的
命中即为主因；后续规则只收集不改变主因的次要原因。

本模块不引入 DSL、注册表或外部依赖——规则只是 Python 函数列表。
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .attribution import AttributionResult
    from ..trace.models import RunTrace
    from .results import EvalResult


class AttributionCategory(StrEnum):
    """失败归因的固定类别；共 16 项（含 UNKNOWN）。"""

    CONTEXT_BUILD_FAILURE = "context_build_failure"
    """上下文构建阶段失败。"""
    CONTEXT_BUDGET_EXCEEDED = "context_budget_exceeded"
    """上下文超出 token 预算。"""
    CONTEXT_INFORMATION_LOST = "context_information_lost"
    """上下文信息丢失（截断/压缩）。"""

    LLM_UNAVAILABLE = "llm_unavailable"
    """LLM 服务不可用或调用失败。"""
    LLM_INVALID_ACTION = "llm_invalid_action"
    """LLM 产生了无效或不可执行的动作。"""
    WRONG_TOOL_SELECTED = "wrong_tool_selected"
    """调用了错误的工具（与期望不符）。"""

    TOOL_ARGUMENT_INVALID = "tool_argument_invalid"
    """工具调用的参数不合法。"""
    TOOL_EXECUTION_FAILED = "tool_execution_failed"
    """工具执行失败。"""

    POLICY_DENIED = "policy_denied"
    """Policy 拒绝了操作。"""
    UNNECESSARY_APPROVAL = "unnecessary_approval"
    """触发了不必要的审批。"""

    APPROVAL_REJECTED = "approval_rejected"
    """审批被拒绝。"""

    DELEGATION_FAILED = "delegation_failed"
    """委派执行失败。"""

    GOAL_NOT_COMPLETED = "goal_not_completed"
    """运行结束但未达成目标。"""
    ITERATION_BUDGET_EXCEEDED = "iteration_budget_exceeded"
    """超出迭代预算。"""

    TOKEN_REGRESSION = "token_regression"
    """Token 使用量超过回归基线。"""

    UNKNOWN = "unknown"
    """证据不足以归因。"""
