"""EvalRunner：执行 EvalCase 并用确定性 Scorer 产出 EvalResult。

执行链（对应开发计划 §4）：

1. 校验 EvalCase 与每条 Expectation 的结构与 options，非法在评分前即归类为
   ``FIXTURE_CONFIGURATION``；
2. 按 ``execution_mode`` 装配 PR3 隔离环境；
3. 执行隔离 Runtime；Fixture 不匹配在 Playback 下抛 ``FixtureConfigurationError``
   （同样归为 ``FIXTURE_CONFIGURATION``），其它异常归为 ``RUNTIME``；
4. 用 PR2 ``assemble_trace`` 重建 Eval Run Trace；重建异常或部分 Trace 未声明允许
   时归为 ``TRACE_RECONSTRUCTION``；
5. 按固定 ``ExpectationKind`` 顺序分派 Scorer，汇总 ``AssertionResult``；
6. 返回 ``EvalResult``，仅当全部期望通过才 ``passed``，否则失败分类为 ``ASSERTION``。
"""

from __future__ import annotations

import re

from .environment import EvalEnvironment
from .fixtures import FixtureConfigurationError
from .models import EvalCase, SCHEMA_VERSION
from .results import AssertionResult, EvalResult, EvaluationFailureKind
from .scorers import SCORERS, ExpectationKind
from ..trace.assembler import assemble_trace


class EvalRunner:
    """执行 ``EvalCase`` 并用确定性 Scorer 产出可追溯 ``EvalResult``。"""

    def __init__(self) -> None:
        """绑定九个确定性 Scorer 实例。"""
        self._scorers: dict[ExpectationKind, object] = SCORERS

    async def run(self, case: EvalCase) -> EvalResult:
        """执行评测用例并返回分类后的结果。"""
        config_errors = self._validate_expectations(case)
        if config_errors:
            return self._config_error(case, config_errors)

        try:
            env = EvalEnvironment(case)
        except (FixtureConfigurationError, ValueError) as exc:
            return self._fail(case.case_id, None, EvaluationFailureKind.FIXTURE_CONFIGURATION, str(exc))

        try:
            outcome = await env.run()
        except FixtureConfigurationError as exc:
            return self._fail(case.case_id, None, EvaluationFailureKind.FIXTURE_CONFIGURATION, str(exc))
        except Exception as exc:
            return self._fail(case.case_id, None, EvaluationFailureKind.RUNTIME, f"{type(exc).__name__}: {exc}")

        try:
            trace = assemble_trace(outcome.run, outcome.events, outcome.messages, outcome.context_versions)
        except Exception as exc:
            return self._fail(case.case_id, outcome.run_id, EvaluationFailureKind.TRACE_RECONSTRUCTION, str(exc))

        if trace.is_partial and not case.allow_partial_trace:
            return EvalResult(
                schema_version=SCHEMA_VERSION,
                case_id=case.case_id,
                run_id=outcome.run_id,
                passed=False,
                assertion_results=(),
                failure_kind=EvaluationFailureKind.TRACE_RECONSTRUCTION,
                failure_detail="运行未完整结束，Trace 部分重建且 Case 未声明允许部分评分",
                trace=trace,
            )

        results: list[AssertionResult] = []
        for expectation in case.expectations:
            scorer = self._scorers[ExpectationKind(expectation.kind)]
            results.append(scorer.score(trace, expectation))

        passed = all(result.passed for result in results)
        return EvalResult(
            schema_version=SCHEMA_VERSION,
            case_id=case.case_id,
            run_id=outcome.run_id,
            passed=passed,
            assertion_results=tuple(results),
            failure_kind=None if passed else EvaluationFailureKind.ASSERTION,
            failure_detail=None if passed else "存在未通过断言",
            trace=trace,
        )

    # ------------------------------------------------------------------ #
    # 校验
    # ------------------------------------------------------------------ #

    def _validate_expectations(self, case: EvalCase) -> list[AssertionResult]:
        """逐条校验期望结构，返回非法期望对应的失败断言。"""
        errors: list[AssertionResult] = []
        for expectation in case.expectations:
            reason = self._validate_one(expectation)
            if reason is not None:
                errors.append(AssertionResult(expectation=expectation, passed=False, evidence=reason))
        return errors

    def _validate_one(self, expectation) -> str | None:
        """校验单条期望的 kind / 字段 / options；返回原因字符串或 None。"""
        try:
            kind = ExpectationKind(expectation.kind)
        except ValueError:
            return f"未知断言类型: {expectation.kind}"
        expected = expectation.expected
        options = expectation.options

        if kind is ExpectationKind.RUN_STATUS:
            allowed = {"completed", "failed", "cancelled", "abandoned", "suspended"}
            if not isinstance(expected, str) or expected.lower() not in allowed:
                return "RUN_STATUS 期望 outcome 必须是 completed/failed/cancelled/abandoned/suspended"
        elif kind is ExpectationKind.TOOL_SEQUENCE:
            if not isinstance(expected, list) or not all(isinstance(item, str) for item in expected):
                return "TOOL_SEQUENCE 期望有序字符串列表"
        elif kind is ExpectationKind.TOOL_ARGUMENT:
            if not isinstance(expected, dict):
                return "TOOL_ARGUMENT 期望参数为对象"
        elif kind is ExpectationKind.APPROVAL:
            allowed = {"approved", "rejected", "waiting", "pending"}
            if not isinstance(expected, str) or expected.lower() not in allowed:
                return "APPROVAL 期望 approved/rejected/waiting/pending"
        elif kind is ExpectationKind.POLICY:
            if not isinstance(expected, str) or expected.lower() not in {"allowed", "denied"}:
                return "POLICY 期望 allowed/denied"
        elif kind is ExpectationKind.OUTPUT_ASSERTION:
            mode = str(options.get("mode", "exact")).lower()
            if mode not in {"exact", "contains", "regex"}:
                return f"OUTPUT_ASSERTION 不支持的 mode={mode}"
            if not isinstance(expected, str):
                return "OUTPUT_ASSERTION 期望文本必须是字符串"
            if mode == "regex":
                try:
                    re.compile(expected)
                except re.error as exc:
                    return f"OUTPUT_ASSERTION 非法正则: {exc}"
        elif kind is ExpectationKind.CONTEXT_RETENTION:
            if not isinstance(expected, str) or not expected:
                return "CONTEXT_RETENTION 期望文本或消息 id 必须是非空字符串"
            try:
                int(expectation.target)
            except (TypeError, ValueError):
                return f"CONTEXT_RETENTION target 必须是上下文版本整数，实际 {expectation.target!r}"
            kind_opt = str(options.get("kind", "text")).lower()
            if kind_opt not in {"text", "message_id"}:
                return f"CONTEXT_RETENTION 不支持的 kind={kind_opt}"
        elif kind is ExpectationKind.TOKEN_BUDGET:
            target = (expectation.target or "tokens_in").lower()
            if target not in {"tokens_in", "tokens_out", "total"}:
                return f"TOKEN_BUDGET 不支持的 target={target}"
            if not isinstance(expected, int) or isinstance(expected, bool) or expected < 0:
                return "TOKEN_BUDGET 期望非负整数上限"
        elif kind is ExpectationKind.ITERATION_BUDGET:
            target = (expectation.target or "llm_calls").lower()
            if target not in {"llm_calls", "tool_calls", "loops"}:
                return f"ITERATION_BUDGET 不支持的 target={target}"
            if not isinstance(expected, int) or isinstance(expected, bool) or expected < 0:
                return "ITERATION_BUDGET 期望非负整数上限"
        return None

    # ------------------------------------------------------------------ #
    # 失败结果构造
    # ------------------------------------------------------------------ #

    @staticmethod
    def _fail(
        case_id: str,
        run_id: str | None,
        kind: EvaluationFailureKind,
        detail: str,
    ) -> EvalResult:
        """构造一个无断言明细的失败结果。"""
        return EvalResult(
            schema_version=SCHEMA_VERSION,
            case_id=case_id,
            run_id=run_id,
            passed=False,
            assertion_results=(),
            failure_kind=kind,
            failure_detail=detail,
            trace=None,
        )

    @staticmethod
    def _config_error(case: EvalCase, errors: list[AssertionResult]) -> EvalResult:
        """构造结构校验失败的结果。"""
        return EvalResult(
            schema_version=SCHEMA_VERSION,
            case_id=case.case_id,
            run_id=None,
            passed=False,
            assertion_results=tuple(errors),
            failure_kind=EvaluationFailureKind.FIXTURE_CONFIGURATION,
            failure_detail="; ".join(error.evidence for error in errors),
            trace=None,
        )
