"""Scorer 协议：九个确定性 Scorer 的统一结构约束。

按开发计划 §2，在 PR4 有九个真实 Scorer 之后才提取这个小 Protocol；它只约束
「读 Trace + 单条 Expectation，产出带证据的 AssertionResult」这一形状，不引入
Scorer Registry，也不要求任何继承——各 Scorer 保持结构化实现即可满足。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..models import Expectation
from ..results import AssertionResult
from ...trace.models import RunTrace
from .kinds import ExpectationKind


@runtime_checkable
class Scorer(Protocol):
    """确定性断言器：只读取 Trace 事实与单条期望，产出可追溯断言结果。"""

    KIND: ExpectationKind

    def score(self, trace: RunTrace, expectation: Expectation) -> AssertionResult:
        """按自身 ``KIND`` 比对 Trace 事实与期望，返回带证据的断言结果。"""
        ...
