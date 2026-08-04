"""期望断言类型枚举。"""

from __future__ import annotations

from enum import StrEnum


class ExpectationKind(StrEnum):
    """九类确定性评分断言的标识符，与 ``Expectation.kind`` 字符串一一对应。"""

    RUN_STATUS = "run_status"
    TOOL_SEQUENCE = "tool_sequence"
    TOOL_ARGUMENT = "tool_argument"
    APPROVAL = "approval"
    POLICY = "policy"
    OUTPUT_ASSERTION = "output_assertion"
    CONTEXT_RETENTION = "context_retention"
    TOKEN_BUDGET = "token_budget"
    ITERATION_BUDGET = "iteration_budget"
