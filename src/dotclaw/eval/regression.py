"""回归报告模型与结果规范化。

``RegressionReport`` 是对一批 ``EvalResult`` 的归因摘要，以三态
（PASS / REGRESSION / ERROR）表达 Dataset 的整体状态。PASS 与
REGRESSION 均以"执行可信"为前提；任何导致执行不可信的错误（基础设施、
数据集损坏、Fixtures 不匹配）归为 ERROR。
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import SCHEMA_VERSION
from .results import EvalResult, EvaluationFailureKind

REPORT_SCHEMA_VERSION: str = SCHEMA_VERSION

_REPORT_STATUS_PASS: str = "PASS"
_REPORT_STATUS_REGRESSION: str = "REGRESSION"
_REPORT_STATUS_ERROR: str = "ERROR"


@dataclass(frozen=True)
class RegressionCaseResult:
    """单个 Case 的回归摘要——只保留不可变的语义信息。"""

    case_id: str
    """Case 标识。"""
    passed: bool
    """单 Case 是否通过（全部 Expectation 达成）。"""
    failure_kind: str | None
    """失败分类（仅 passed=False 时有意义）。"""
    failure_detail: str | None
    """失败详情（截断至 500 字符以控制报告大小）。"""

    def to_dict(self) -> dict[str, object]:
        """序列化为 JSON 兼容字典。"""
        return {
            "case_id": self.case_id,
            "passed": self.passed,
            "failure_kind": self.failure_kind,
            "failure_detail": self.failure_detail,
        }

    @classmethod
    def from_result(cls, result: EvalResult) -> RegressionCaseResult:
        """从单次评测结果构造摘要。"""
        detail: str | None = _truncate(result.failure_detail, 500)
        return cls(
            case_id=result.case_id,
            passed=result.passed,
            failure_kind=None if result.failure_kind is None else result.failure_kind.value,
            failure_detail=detail,
        )


@dataclass(frozen=True)
class RegressionReport:
    """Dataset 回归执行的完整报告。

    ``overall_status`` 固定为 PASS / REGRESSION / ERROR 三者之一；
    PASS 要求所有受信结果均通过；REGRESSION 表示至少一条断言失败；
    ERROR 表示评测基础设施未能产生受信结果。
    """

    schema_version: str
    """报告 schema 版本（与 Case schema 同源）。"""
    dataset: str
    """被评估的 Dataset 标识。"""
    overall_status: str
    """PASS | REGRESSION | ERROR。"""
    case_results: tuple[RegressionCaseResult, ...]
    """每个 Case 的归一化摘要。"""
    error_detail: str | None = None
    """仅 overall_status=ERROR 时携带的诊断信息。"""

    def to_dict(self) -> dict[str, object]:
        """序列化为 JSON 兼容字典。"""
        return {
            "schema_version": self.schema_version,
            "dataset": self.dataset,
            "overall_status": self.overall_status,
            "case_results": [item.to_dict() for item in self.case_results],
            "error_detail": self.error_detail,
        }

    @property
    def passed(self) -> bool:
        """报告是否代表通过状态（向后兼容 Gate 使用）。"""
        return self.overall_status == _REPORT_STATUS_PASS


def normalize_result(result: EvalResult) -> EvalResult:
    """规范化 EvalResult，剥离 run_id 用于可比较场景。

    Playback 的每次执行产生新的 run_id，但其余语义应不变；
    本函数将 run_id 替换为固定哨兵，保留其余字段不变。
    """
    from dataclasses import replace

    return replace(result, run_id="<normalized>")


# ---------------------------------------------------------------------------
# 内部辅助
# ---------------------------------------------------------------------------


def _truncate(text: str | None, max_length: int) -> str | None:
    """截断文本至指定长度，超出追加省略标记。"""
    if text is None:
        return None
    if len(text) <= max_length:
        return text
    return text[:max_length] + "…"


def _is_trusted(result: EvalResult) -> bool:
    """判端执行是否为"受信"结果——运行完整且 Fixture 匹配，只是断言可能失败。"""
    if result.failure_kind is None:
        return True
    return result.failure_kind is EvaluationFailureKind.ASSERTION
