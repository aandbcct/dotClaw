"""回归闸门：对 Playback 结果归因并产出三态报告。

``RegressionGate`` 只信任 Playback（STRICT 匹配、Frozen Agent）产生的结果；
任何基础设施错误（Dataset 损坏、Fixture 不匹配、Trace 重建失败）归入 ERROR，
断言失败归入 REGRESSION。Re-execution 结果不得传入 Gate。
"""

from __future__ import annotations

from .regression import (
    REPORT_SCHEMA_VERSION,
    PlaybackBatch,
    RegressionCaseResult,
    RegressionReport,
    _is_trusted,
    _REPORT_STATUS_ERROR,
    _REPORT_STATUS_PASS,
    _REPORT_STATUS_REGRESSION,
)
from .results import EvalResult, EvaluationFailureKind

# 判定为"受信但断言失败"的 failure_kind 集合——只有这些情况归为 REGRESSION。
_TRUSTED_FAILURE_KINDS: frozenset[EvaluationFailureKind] = frozenset(
    {EvaluationFailureKind.ASSERTION}
)


class RegressionGate:
    """Playback 回归闸门：仅接受 ``PlaybackBatch`` 并对结果判定三态。

    ``evaluate()`` 的入参类型为 ``PlaybackBatch``——该类型仅由
    ``PlaybackRunner`` 产出，Re-execution 结果无法绕过。Gate 不从
    EvalResult 中"重算"通过与否，直接复用 ``EvalResult.passed``；
    只对不可信的运行基础设施错误定位并阻止 CI。
    """

    def evaluate(self, batch: PlaybackBatch) -> RegressionReport:
        """对 PlaybackBatch 归因并产出报告。

        判定规则：
        - 全部受信且 passed → PASS
        - 至少一条受信但断言失败 → REGRESSION
        - 任何一条不可信（RUNTIME / FIXTURE_CONFIGURATION / TRACE_RECONSTRUCTION）→ ERROR
        """
        results = batch.results
        dataset = batch.dataset
        if not results:
            return RegressionReport(
                schema_version=REPORT_SCHEMA_VERSION,
                dataset=dataset,
                overall_status=_REPORT_STATUS_ERROR,
                case_results=(),
                error_detail="Dataset 未产出任何评测结果",
            )

        case_summaries: list[RegressionCaseResult] = []
        has_error: bool = False
        has_regression: bool = False
        error_detail: str | None = None

        for result in results:
            case_summaries.append(RegressionCaseResult.from_result(result))
            if not _is_trusted(result):
                has_error = True
                if error_detail is None:
                    error_detail = (
                        f"Case {result.case_id!r} 产生不可信结果："
                        f"{result.failure_kind.value if result.failure_kind else 'unknown'}"
                        + (f" — {result.failure_detail}" if result.failure_detail else "")
                    )
                # 继续收集后续摘要，完整记录所有 Case 状态
            elif not result.passed:
                has_regression = True

        if has_error:
            status = _REPORT_STATUS_ERROR
        elif has_regression:
            status = _REPORT_STATUS_REGRESSION
        else:
            status = _REPORT_STATUS_PASS

        return RegressionReport(
            schema_version=REPORT_SCHEMA_VERSION,
            dataset=dataset,
            overall_status=status,
            case_results=tuple(case_summaries),
            error_detail=error_detail if status == _REPORT_STATUS_ERROR else None,
        )
