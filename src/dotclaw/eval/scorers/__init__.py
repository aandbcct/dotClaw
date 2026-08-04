"""九个确定性 Scorer 与固定分派注册表。

``SCORERS`` 把 ``ExpectationKind`` 映射到其实例；``EvalRunner`` 按期望的 ``kind``
直接查表分派，不建立可扩展注册表（按开发计划要求仅在 PR4 提取这个小协议）。
"""

from __future__ import annotations

from .approval import ApprovalScorer
from .context_retention import ContextRetentionScorer
from .iteration_budget import IterationBudgetScorer
from .kinds import ExpectationKind
from .output_assertion import OutputAssertionScorer
from .policy import PolicyScorer
from .run_status import RunStatusScorer
from .token_budget import TokenBudgetScorer
from .tool_arguments import ToolArgumentScorer
from .tool_sequence import ToolSequenceScorer

ALL_SCORERS: list[type] = [
    RunStatusScorer,
    ToolSequenceScorer,
    ToolArgumentScorer,
    ApprovalScorer,
    PolicyScorer,
    OutputAssertionScorer,
    ContextRetentionScorer,
    TokenBudgetScorer,
    IterationBudgetScorer,
]

SCORERS: dict[ExpectationKind, object] = {scorer.KIND: scorer() for scorer in ALL_SCORERS}

__all__ = [
    "ExpectationKind",
    "ALL_SCORERS",
    "SCORERS",
    "RunStatusScorer",
    "ToolSequenceScorer",
    "ToolArgumentScorer",
    "ApprovalScorer",
    "PolicyScorer",
    "OutputAssertionScorer",
    "ContextRetentionScorer",
    "TokenBudgetScorer",
    "IterationBudgetScorer",
]
