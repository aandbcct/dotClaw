"""ReexecutionRunner：以当前 Agent / Prompt / LLM 重新执行 Dataset。

与 Playback 不同：每个 Case 的 ``execution_mode`` 被覆写为
``ExecutionMode.REEXECUTION``（NORMAL 匹配），允许 Fixture 缺失时
回退到真实端口。结果仅供人工比较，不得传入 Gate。
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

from .dataset import load_cases
from .models import EvalCaseValidationError, ExecutionMode
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


class ReexecutionRunner:
    """Re-execution 批量执行器：以 REEXECUTION 模式执行 Dataset 全部 Case。

    Case 的执行模式被覆写为 REEXECUTION，保留 Conversation 与隔离外部
    Fixture，允许 NORMAL 匹配与真实 LLM 回退。不调用 retry_interrupted，
    不写原 Run / Session / 工作目录，不使用生产凭证。
    """

    async def run_dataset(self, root: Path, dataset_name: str) -> tuple[EvalResult, ...]:
        """加载并以 REEXECUTION 模式逐个执行 Dataset 全部 Case。

        参数：
            root: Dataset 根目录。
            dataset_name: Dataset 名称。

        返回：
            按 Case 加载顺序对应的评测结果元组。
        """
        results: list[EvalResult] = []
        runner = EvalRunner()

        try:
            cases = load_cases(root, dataset_name)
        except (EvalCaseValidationError, FileNotFoundError) as e:
            return (_error_result("", str(e)),)

        if not cases:
            return (_error_result("", f"Dataset {dataset_name!r} 未包含任何 Case"),)

        for case in cases:
            # REEXECUTION 覆写：保留 Conversation 与隔离 Fixture，允许真实 LLM
            reexec_case = dataclasses.replace(case, execution_mode=ExecutionMode.REEXECUTION)
            try:
                result = await runner.run(reexec_case)
            except Exception as e:
                result = _error_result(
                    case.case_id,
                    f"Case {case.case_id!r} 执行异常：{type(e).__name__}: {e}",
                )
            results.append(result)

        return tuple(results)
