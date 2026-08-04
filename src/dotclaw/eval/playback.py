"""PlaybackRunner：批量以冻结回放模式执行 Dataset 全部 Case。

每个 Case 创建独立 InMemory Run，复用 PR3 隔离环境与 PR4 EvalRunner；
强制 PLAYBACK / STRICT 匹配，禁止注入真实依赖。加载或执行错误记录为
不可信结果，交由上游 Gate 判定为 ERROR。
"""

from __future__ import annotations

from pathlib import Path

from .dataset import load_cases
from .gate import RegressionGate
from .models import EvalCaseValidationError
from .regression import RegressionReport
from .results import EvalResult, EvaluationFailureKind
from .runner import EvalRunner


def _error_result(case_id: str, detail: str) -> EvalResult:
    """构造不可信的加载/执行错误结果。"""
    return EvalResult(
        schema_version="1.0",
        case_id=case_id,
        run_id=None,
        passed=False,
        assertion_results=(),
        failure_kind=EvaluationFailureKind.FIXTURE_CONFIGURATION,
        failure_detail=detail,
    )


class PlaybackRunner:
    """Playback 批量执行器：加载 Dataset 全部 Case 并以 PLAYBACK 模式执行。

    每个 Case 以独立 InMemory Run 执行，结果收集为元组供 Gate 或比较使用。
    """

    async def run_dataset(self, root: Path, dataset_name: str) -> tuple[EvalResult, ...]:
        """加载并逐个执行 Dataset 中全部 Case。

        参数：
            root: Dataset 根目录（含 cases/ 子目录的父级）。
            dataset_name: Dataset 名称（目录名）。

        返回：
            按 Case 加载顺序对应的评测结果元组；加载阶段错误将返回单条不可信结果。
        """
        results: list[EvalResult] = []
        runner = EvalRunner()

        try:
            cases = load_cases(root, dataset_name)
        except EvalCaseValidationError as e:
            return (_error_result("", f"Dataset {dataset_name!r} 加载失败：{e}"),)
        except FileNotFoundError as e:
            return (_error_result("", f"Dataset {dataset_name!r} 目录不存在：{e}"),)

        if not cases:
            return (_error_result("", f"Dataset {dataset_name!r} 未包含任何 Case"),)

        for case in cases:
            try:
                result = await runner.run(case)
            except Exception as e:
                result = _error_result(
                    case.case_id,
                    f"Case {case.case_id!r} 执行异常：{type(e).__name__}: {e}",
                )
            results.append(result)

        return tuple(results)

    async def run_and_gate(
        self,
        root: Path,
        dataset_name: str,
    ) -> RegressionReport:
        """执行数据集并产出 Gate 判定报告。

        等价于 ``run_dataset()`` 后接 ``RegressionGate().evaluate()``，
        是 CI 场景的最常用入口。
        """
        results = await self.run_dataset(root, dataset_name)
        gate = RegressionGate()
        return gate.evaluate(results, dataset=dataset_name)
