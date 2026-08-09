"""PR8 业务基线编排与工件测试。"""

import json

import pytest

from benchmarks.business_baseline import BusinessDatasetError, load_business_documents, run_fixture_dataset
from dotclaw.eval.dataset import load_case
from dotclaw.eval.reexecution import ReexecutionRunner


def test_runtime_core_v2_has_frozen_task_shape() -> None:
    """任务集必须固定为八个 Case、两个工作流。"""
    cases, workflows = load_business_documents(__import__("pathlib").Path("benchmarks/datasets"), "runtime_core_v2")
    assert len(cases) == 8 and len(workflows) == 2


@pytest.mark.asyncio
async def test_all_standard_tasks_are_independent_executable_v2_cases() -> None:
    """八项标准任务必须直接消费各自的 v2 Case，而非映射回 v1 旧 Case。"""
    root = __import__("pathlib").Path("benchmarks/datasets")
    cases, _ = load_business_documents(root, "runtime_core_v2")
    runner = ReexecutionRunner()

    for task_id, document in cases.items():
        case = load_case(root, "runtime_core_v2", task_id)
        result = await runner.run_case(case)
        assert document["task_id"] == case.case_id
        assert result.passed, f"{task_id}: {result.failure_detail}"


@pytest.mark.asyncio
async def test_fixture_run_writes_traceable_artifacts(tmp_path) -> None:
    """Fixture 开发运行也必须写出 JSONL、快照、配置和任务报告。"""
    snapshot = await run_fixture_dataset(__import__("pathlib").Path("benchmarks/datasets"), "runtime_core_v2", warmup=0, repeat=1, output=tmp_path, baseline=tmp_path / "baseline")
    assert snapshot.global_summary.sample_count == 10
    assert (tmp_path / snapshot.samples_path).is_file()
    assert (tmp_path / f"{snapshot.snapshot_id}.json").is_file()
    assert (tmp_path / "business-config.json").is_file()
    assert len((tmp_path / snapshot.samples_path).read_text(encoding="utf-8").splitlines()) == 10
    assert json.loads((tmp_path / f"{snapshot.snapshot_id}.json").read_text(encoding="utf-8"))["dataset"] == "runtime_core_v2"
