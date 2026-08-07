"""ReexecutionRunner：以当前 Agent / Prompt / LLM 重新执行 Dataset。

与 Playback 不同：每个 Case 的 ``execution_mode`` 被覆写为
``ExecutionMode.REEXECUTION``（NORMAL 匹配），允许 Fixture 缺失时
回退到真实端口。结果仅供人工比较，不得传入 Gate。
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import TYPE_CHECKING

from .dataset import load_cases
from .models import EvalCaseValidationError, ExecutionMode
from .results import EvalResult, EvaluationFailureKind
from .runner import EvalRunner

if TYPE_CHECKING:
    from .environment import EvalDependencies


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


class ReexecutionRunner:
    """Re-execution 批量执行器：以 REEXECUTION 模式执行 Dataset 全部 Case。

    Case 的执行模式被覆写为 REEXECUTION，保留 Conversation 与隔离外部
    Fixture，允许 NORMAL 匹配与真实 LLM 回退。不调用 retry_interrupted，
    不写原 Run / Session / 工作目录，不使用生产凭证。

    通过 ``dependencies`` 注入当前 Agent / Prompt / LLM 的真实端口；
    未注入时仅消费 Fixture（等价于松弛匹配的 Playback）。
    """

    def __init__(self, dependencies: "EvalDependencies | None" = None) -> None:
        """绑定 Re-execution 所需的生产端口。"""
        self._deps: EvalDependencies | None = dependencies

    async def run_case(self, case: EvalCase) -> EvalResult:
        """以 REEXECUTION 模式执行单个 Case。

        覆写 Case 为既有的 ``ExecutionMode.REEXECUTION`` 后调用 ``EvalRunner``，
        不改变 Fixture、依赖注入、隔离 Repository 或真实依赖回退的既有语义。
        Benchmark 用该入口对每个 Case 计量真实端到端耗时，避免复制覆写与
        错误分类逻辑。

        参数：
            case: 要执行的评测用例。

        返回：
            单条评测结果；执行异常时转为不可信的错误结果。
        """
        # REEXECUTION 覆写：保留 Conversation 与隔离 Fixture，允许真实 LLM
        reexec_case = dataclasses.replace(case, execution_mode=ExecutionMode.REEXECUTION)
        runner = EvalRunner()
        try:
            return await runner.run(reexec_case, dependencies=self._deps)
        except Exception as e:
            return _error_result(
                case.case_id,
                f"Case {case.case_id!r} 执行异常：{type(e).__name__}: {e}",
            )

    async def run_dataset(self, root: Path, dataset_name: str) -> tuple[EvalResult, ...]:
        """加载并以 REEXECUTION 模式逐个执行 Dataset 全部 Case。

        复用 ``run_case()`` 保持单条执行与错误转换行为一致，按 Case 加载顺序
        返回对应的评测结果元组。

        参数：
            root: Dataset 根目录。
            dataset_name: Dataset 名称。

        返回：
            按 Case 加载顺序对应的评测结果元组。
        """
        results: list[EvalResult] = []

        try:
            cases = load_cases(root, dataset_name)
        except (EvalCaseValidationError, FileNotFoundError) as e:
            return (_error_result("", str(e)),)

        if not cases:
            return (_error_result("", f"Dataset {dataset_name!r} 未包含任何 Case"),)

        for case in cases:
            results.append(await self.run_case(case))

        return tuple(results)
