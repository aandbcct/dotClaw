"""评测结果与失败分类模型。

``EvalResult`` 是 PR4 对一次 ``EvalCase`` 执行的唯一产出：它聚合各条 ``Expectation``
的 ``AssertionResult``，并以互斥的 ``EvaluationFailureKind`` 表达失败归属——运行本身
不可信（RUNTIME / TRACE_RECONSTRUCTION / FIXTURE_CONFIGURATION）与“已可信执行但行为
不符合预期”（ASSERTION）必须明确区分，后者才是评分意义上的失败。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from .models import Expectation, SCHEMA_VERSION

if TYPE_CHECKING:
    from ..trace.models import RunTrace


class EvaluationFailureKind(StrEnum):
    """评测失败的四类互斥分类。"""

    RUNTIME = "runtime"
    """隔离 Runtime 执行抛出了非配置性的异常，结果不可信。"""

    TRACE_RECONSTRUCTION = "trace_reconstruction"
    """运行事实无法重建为完整、可评分的 RunTrace。"""

    FIXTURE_CONFIGURATION = "fixture_configuration"
    """Case / Expectation 结构非法，或 Fixture 未匹配导致执行不可信。"""

    ASSERTION = "assertion"
    """运行已可信执行，但行为与某条期望不符。"""


@dataclass(frozen=True)
class AssertionResult:
    """单条断言的通过与否与可追溯证据。"""

    expectation: Expectation
    passed: bool
    evidence: str

    def to_dict(self) -> dict[str, object]:
        """序列化为稳定字典。"""
        return {
            "kind": self.expectation.kind,
            "target": self.expectation.target,
            "passed": self.passed,
            "evidence": self.evidence,
        }


@dataclass(frozen=True)
class EvalResult:
    """一次评测执行的完整结果。"""

    schema_version: str
    case_id: str
    run_id: str | None
    passed: bool
    assertion_results: tuple[AssertionResult, ...]
    failure_kind: EvaluationFailureKind | None = None
    failure_detail: str | None = None
    trace: "RunTrace | None" = None

    def to_dict(self) -> dict[str, object]:
        """序列化为 JSON 兼容字典（不内联完整 Trace 内容）。"""
        return {
            "schema_version": self.schema_version,
            "case_id": self.case_id,
            "run_id": self.run_id,
            "passed": self.passed,
            "failure_kind": None if self.failure_kind is None else self.failure_kind.value,
            "failure_detail": self.failure_detail,
            "assertion_results": [result.to_dict() for result in self.assertion_results],
            "trace_available": self.trace is not None,
        }
