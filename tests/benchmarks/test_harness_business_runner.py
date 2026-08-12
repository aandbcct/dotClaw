"""PR8 Agent Harness 配对执行与工件测试。"""

import json
from pathlib import Path

import pytest

from benchmarks.harness_business_dataset import ExecutionCondition, HarnessTaskInstance, load_harness_dataset
from benchmarks.harness_business_runner import HarnessBusinessRunError, HarnessExecutionResult, _required_checks, run_harness_business_matrix


class _Executor:
    """返回完整确定性观察的开发执行替身。"""

    def __init__(self) -> None:
        self.preflights = 0
        self.samples = 0

    async def execute(self, instance, condition, *, attempt: int, preflight: bool) -> HarnessExecutionResult:
        """为每个任务返回可追溯的稳定候选。"""
        del condition, attempt
        if preflight:
            self.preflights += 1
        else:
            self.samples += 1
        candidate = "；".join((*instance.allowed_facts, *instance.required_constraints, instance.expected_delivery))
        return HarnessExecutionResult(candidate, True, frozenset(instance.deterministic_checks), f"run-{self.preflights}-{self.samples}", True, 10.0, 1, 1)


class _Judge:
    """按 Prompt 中原子判据逐项通过的开发 Judge 替身。"""

    def __init__(self) -> None:
        self.calls = 0

    async def judge(self, prompt: str) -> str:
        """返回严格 JSON，不读取外部知识。"""
        self.calls += 1
        payload = json.loads(prompt.split("\n", 1)[1])
        criteria = {item["id"]: "pass" for item in payload["criteria"]}
        return json.dumps({"verdict": "pass", "criteria": criteria, "reason": "开发替身"}, ensure_ascii=False)


class _SensitiveReviewJudge(_Judge):
    """返回含凭证模式理由的脱敏验证 Judge 替身。"""

    async def judge(self, prompt: str) -> str:
        """保持协议有效，同时在理由中放入应被脱敏的 Bearer Token。"""
        self.calls += 1
        payload = json.loads(prompt.split("\n", 1)[1])
        criteria = {item["id"]: "pass" for item in payload["criteria"]}
        return json.dumps(
            {"verdict": "pass", "criteria": criteria, "reason": "复核凭证 Bearer abcdefghijklmnop"},
            ensure_ascii=False,
        )


class _SensitiveDeliveryExecutor(_Executor):
    """返回包含敏感赋值模式的候选交付替身。"""

    async def execute(self, instance, condition, *, attempt: int, preflight: bool) -> HarnessExecutionResult:
        """Preflight 沿用普通候选，正式样本用于验证持久化脱敏。"""
        result = await super().execute(instance, condition, attempt=attempt, preflight=preflight)
        if preflight:
            return result
        return HarnessExecutionResult(
            result.candidate + "；api_key=super-secret-value",
            result.deterministic_passed,
            result.observed_checks,
            result.run_id,
            result.trace_available,
            result.wall_duration_ms,
            result.llm_call_count,
            result.tool_call_count,
        )


class _FailedPreflightExecutor(_Executor):
    """模拟某个正式执行条件无法生成 Trace 的执行替身。"""

    async def execute(self, instance, condition, *, attempt: int, preflight: bool) -> HarnessExecutionResult:
        """Preflight 返回缺失 Trace；正式样本不应开始。"""
        if preflight:
            self.preflights += 1
            return HarnessExecutionResult("", False, frozenset(), None, False, 1.0, 0, 0)
        return await super().execute(instance, condition, attempt=attempt, preflight=preflight)


class _EmptyDeliveryExecutor(_Executor):
    """模拟 Runtime 已收口但没有最终用户交付的执行替身。"""

    async def execute(self, instance, condition, *, attempt: int, preflight: bool) -> HarnessExecutionResult:
        """保留确定性事实，但返回空候选。"""
        if preflight:
            return await super().execute(instance, condition, attempt=attempt, preflight=preflight)
        self.samples += 1
        return HarnessExecutionResult("", True, frozenset(instance.deterministic_checks), f"run-empty-{self.samples}", True, 1.0, 1, 0)


@pytest.mark.asyncio
async def test_full_matrix_writes_paired_snapshots_and_report(tmp_path: Path) -> None:
    """完整开发矩阵必须写出 180/180 双快照、七次 preflight 和配对报告。"""
    dataset = load_harness_dataset(Path("benchmarks/datasets"))
    executor, judge = _Executor(), _Judge()

    full, baseline = await run_harness_business_matrix(
        dataset,
        executor,
        judge,
        output=tmp_path,
        provider="provider",
        model="model",
        temperature=0.0,
        judge_provider="provider",
        judge_model="judge",
        formal_sampling=False,
    )

    assert full.global_summary.sample_count == 180
    assert baseline.global_summary.sample_count == 180
    assert full.global_summary.llm_call_count_total == 180
    assert full.global_summary.tool_call_count_total == 180
    assert executor.preflights == 7
    assert executor.samples == 360
    assert judge.calls == 360
    assert len((tmp_path / full.samples_path).read_text(encoding="utf-8").splitlines()) == 180
    assert len((tmp_path / baseline.samples_path).read_text(encoding="utf-8").splitlines()) == 180
    assert full.samples_content_summary["line_count"] == 180
    assert full.samples_content_summary["byte_count"] > 0
    assert len(full.samples_content_summary["sha256"]) == 64
    assert (tmp_path / "harness-business-summary.json").is_file()
    assert json.loads((tmp_path / "business-config.json").read_text(encoding="utf-8"))["formal_sampling"] == "false"
    first_sample = json.loads((tmp_path / full.samples_path).read_text(encoding="utf-8").splitlines()[0])
    assert first_sample["evidence_summary"]["deterministic_missing"] == []
    assert first_sample["evidence_summary"]["deterministic_required"]
    assert first_sample["candidate_delivery_redacted"]
    assert first_sample["judge_reason_redacted"] == "开发替身"
    assert first_sample["review_redaction_applied"] is False
    assert first_sample["workflow_version"] == "2"
    assert full.environment["workflow_version"] == "2"


@pytest.mark.asyncio
async def test_review_text_is_persisted_only_after_redaction(tmp_path: Path) -> None:
    """候选交付和 Judge 理由必须脱敏后写入 JSONL，禁止凭证原文落盘。"""
    dataset = load_harness_dataset(Path("benchmarks/datasets"))
    instance = dataset.instances[0]

    full, _ = await run_harness_business_matrix(
        dataset,
        _SensitiveDeliveryExecutor(),
        _SensitiveReviewJudge(),
        output=tmp_path,
        provider="provider",
        model="model",
        temperature=0.0,
        judge_provider="provider",
        judge_model="judge",
        formal_sampling=False,
        repeat=1,
        instance_ids=(instance.instance_id,),
    )
    payload = (tmp_path / full.samples_path).read_text(encoding="utf-8")
    sample = json.loads(payload)

    assert "super-secret-value" not in payload
    assert "abcdefghijklmnop" not in payload
    assert sample["candidate_delivery_redacted"].endswith("api_key=[redacted]")
    assert sample["judge_reason_redacted"].endswith("[redacted]")
    assert sample["review_redaction_applied"] is True


@pytest.mark.asyncio
async def test_diagnostic_subset_does_not_write_formal_business_report(tmp_path: Path) -> None:
    """单实例开发诊断不得生成可被引用的 Harness Value 报告。"""
    dataset = load_harness_dataset(Path("benchmarks/datasets"))
    instance = dataset.instances[0]

    full, baseline = await run_harness_business_matrix(
        dataset,
        _Executor(),
        _Judge(),
        output=tmp_path,
        provider="provider",
        model="model",
        temperature=0.0,
        judge_provider="provider",
        judge_model="judge",
        formal_sampling=False,
        repeat=1,
        instance_ids=(instance.instance_id,),
    )

    assert full.global_summary.sample_count == 1
    assert baseline.global_summary.sample_count == 1
    assert (tmp_path / "diagnostic-summary.json").is_file()
    assert not (tmp_path / "harness-business-summary.json").exists()


@pytest.mark.asyncio
async def test_formal_run_rejects_subset_before_execution(tmp_path: Path) -> None:
    """正式标记不能用于单实例或错误重复次数。"""
    dataset = load_harness_dataset(Path("benchmarks/datasets"))

    with pytest.raises(HarnessBusinessRunError, match="全部 60 个实例"):
        await run_harness_business_matrix(
            dataset,
            _Executor(),
            _Judge(),
            output=tmp_path,
            provider="provider",
            model="model",
            temperature=0.0,
            judge_provider="provider",
            judge_model="judge",
            formal_sampling=True,
            repeat=1,
            instance_ids=(dataset.instances[0].instance_id,),
        )


@pytest.mark.asyncio
async def test_formal_run_rejects_failed_preflight_before_samples(tmp_path: Path) -> None:
    """任何执行条件未到达 Runtime/Trace 时，正式矩阵必须在计分前失败。"""
    dataset = load_harness_dataset(Path("benchmarks/datasets"))
    executor = _FailedPreflightExecutor()

    with pytest.raises(HarnessBusinessRunError, match="Preflight"):
        await run_harness_business_matrix(
            dataset,
            executor,
            _Judge(),
            output=tmp_path,
            provider="provider",
            model="model",
            temperature=0.0,
            judge_provider="provider",
            judge_model="judge",
            formal_sampling=True,
            timeout_seconds=60.0,
            retry_count=0,
        )

    assert executor.samples == 0


def test_baseline_checks_remove_only_ablated_mechanism() -> None:
    """Baseline 不得因缺少被移除机制而预先失败，也不能丢掉业务副作用断言。"""
    dataset = load_harness_dataset(Path("benchmarks/datasets"))
    research = next(item for item in dataset.instances if item.instance_id == "evidence-01-vendor-decision")
    workspace = next(item for item in dataset.instances if item.instance_id == "workspace-03-test-regression")

    assert _required_checks(research, ExecutionCondition.SINGLE_PASS_RESEARCH) == {"runtime_completed"}
    workspace_checks = _required_checks(workspace, ExecutionCondition.SINGLE_AGENT_WORKSPACE)
    assert "file_modified" in workspace_checks
    assert "targeted_test_passed" in workspace_checks


@pytest.mark.asyncio
async def test_missing_final_delivery_is_a_business_failure(tmp_path: Path) -> None:
    """Runtime 已完成但无最终反馈时应归因交付缺失，而不是 Fixture 故障。"""
    dataset = load_harness_dataset(Path("benchmarks/datasets"))
    instance = dataset.instances[0]

    full, _ = await run_harness_business_matrix(
        dataset,
        _EmptyDeliveryExecutor(),
        _Judge(),
        output=tmp_path,
        provider="provider",
        model="model",
        temperature=0.0,
        judge_provider="provider",
        judge_model="judge",
        formal_sampling=False,
        repeat=1,
        instance_ids=(instance.instance_id,),
    )
    sample = json.loads((tmp_path / full.samples_path).read_text(encoding="utf-8"))

    assert sample["task_success"] is False
    assert sample["failure_attribution"] == "missing_final_delivery"
