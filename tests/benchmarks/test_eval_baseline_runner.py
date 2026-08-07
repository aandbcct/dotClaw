"""PR1 EvalBaselineRunner 与 CLI：正常路径、边界路径与数据损坏防护。"""

from __future__ import annotations

import dataclasses
import json
import shutil
from pathlib import Path

import pytest

from benchmarks.eval_baseline import (
    BaselineExperimentError,
    EvalBaselineRunner,
    build_markdown_report,
    config_hash,
    git_short_commit,
    make_snapshot_id,
)
from benchmarks.eval_baseline_models import BENCHMARK_SCHEMA_VERSION, BenchmarkSample, BenchmarkSnapshot
from dotclaw.eval.reexecution import ReexecutionRunner
from dotclaw.eval.results import EvaluationFailureKind

from .helpers import make_sample

_CASE_IDS = ("approval_rejected", "approval_resume", "context_retention", "tool_success")
_DATASET = "runtime_core_v1"
_REPO_CASES = Path(__file__).resolve().parents[2] / "benchmarks" / "datasets" / "runtime_core_v1" / "cases"


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #


def _seed_dataset(root: Path, case_ids: tuple[str, ...] = _CASE_IDS) -> None:
    """把仓库内固化的真实 Case 复制到临时 Dataset。"""
    cases_dir = root / _DATASET / "cases"
    cases_dir.mkdir(parents=True)
    for case_id in case_ids:
        shutil.copy2(_REPO_CASES / f"{case_id}.json", cases_dir / f"{case_id}.json")


async def _run(root: Path, *, warmup: int, repeat: int, baseline: bool = False) -> BenchmarkSnapshot:
    """在临时目录执行一次基线运行。"""
    output_dir = root / "reports"
    baseline_dir = root / "baselines" if baseline else None
    return await EvalBaselineRunner().run_dataset(
        root,
        _DATASET,
        warmup=warmup,
        repeat=repeat,
        output_dir=output_dir,
        baseline_dir=baseline_dir,
    )


# --------------------------------------------------------------------------- #
# 正常路径
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_run_dataset_end_to_end(tmp_path: Path) -> None:
    """四个 Case 稳定加载并重复执行，写出 JSONL / JSON / Markdown 三件工件。"""
    _seed_dataset(tmp_path)
    snapshot = await _run(tmp_path, warmup=0, repeat=2)

    assert snapshot.dataset == _DATASET
    assert snapshot.git_commit == git_short_commit()
    assert [case.case_id for case in snapshot.cases] == list(_CASE_IDS)
    assert all(case.sample_count == 2 for case in snapshot.cases)
    assert snapshot.global_summary.sample_count == 8
    assert snapshot.global_summary.passed_count == 8

    # JSONL：非 warmup 每次执行产生一条可反序列化记录
    samples_path = tmp_path / "reports" / "samples" / f"{snapshot.snapshot_id}.jsonl"
    snapshot_path = tmp_path / "reports" / f"{snapshot.snapshot_id}.json"
    report_path = tmp_path / "reports" / f"{snapshot.snapshot_id}.md"
    assert samples_path.exists()
    assert snapshot_path.exists()
    assert report_path.exists()

    lines = samples_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 8
    for line in lines:
        sample = BenchmarkSample.from_dict(json.loads(line))
        assert sample.schema_version == BENCHMARK_SCHEMA_VERSION
        assert sample.is_warmup is False
        assert sample.trace_available is True

    # JSON 快照可重新读取且与原快照等价
    restored = BenchmarkSnapshot.from_dict(json.loads(snapshot_path.read_text(encoding="utf-8")))
    assert restored.to_dict() == snapshot.to_dict()

    # 内容摘要已回填
    assert snapshot.samples_content_summary["line_count"] == 8


@pytest.mark.asyncio
async def test_real_repo_dataset_loads_and_runs(tmp_path: Path) -> None:
    """仓库内固化的 runtime_core_v1 Dataset 可直接加载并重复执行。"""
    root = Path(__file__).resolve().parents[2] / "benchmarks" / "datasets"
    output_dir = tmp_path / "reports"
    snapshot = await EvalBaselineRunner().run_dataset(
        root, _DATASET, warmup=0, repeat=1, output_dir=output_dir
    )
    assert [case.case_id for case in snapshot.cases] == list(_CASE_IDS)
    assert snapshot.global_summary.passed_count == 4
    assert snapshot.global_summary.success_rate == 1.0


@pytest.mark.asyncio
async def test_warmup_not_in_stats(tmp_path: Path) -> None:
    """warmup 样本写 JSONL 但不进入正式统计。"""
    _seed_dataset(tmp_path)
    snapshot = await _run(tmp_path, warmup=1, repeat=2)

    samples_path = tmp_path / "reports" / "samples" / f"{snapshot.snapshot_id}.jsonl"
    lines = samples_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 4 * 3  # 4 Case × (1 warmup + 2 formal)
    warmup_lines = [line for line in lines if json.loads(line)["is_warmup"] is True]
    formal_lines = [line for line in lines if json.loads(line)["is_warmup"] is False]
    assert len(warmup_lines) == 4
    assert len(formal_lines) == 8
    assert all(case.sample_count == 2 for case in snapshot.cases)
    assert snapshot.global_summary.sample_count == 8


@pytest.mark.asyncio
async def test_baseline_dir_layout(tmp_path: Path) -> None:
    """提交基线目录写出 <snapshot-id>.json 与 samples/<snapshot-id>.jsonl。"""
    _seed_dataset(tmp_path)
    snapshot = await _run(tmp_path, warmup=0, repeat=1, baseline=True)

    baseline_json = tmp_path / "baselines" / f"{snapshot.snapshot_id}.json"
    baseline_jsonl = tmp_path / "baselines" / "samples" / f"{snapshot.snapshot_id}.jsonl"
    assert baseline_json.exists()
    assert baseline_jsonl.exists()
    assert BenchmarkSnapshot.from_dict(json.loads(baseline_json.read_text(encoding="utf-8"))) == snapshot
    # 快照引用的原始记录路径是相对于自身目录的
    assert snapshot.samples_path == f"samples/{snapshot.snapshot_id}.jsonl"


# --------------------------------------------------------------------------- #
# 边界路径
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_repeat_zero_and_negative_rejected(tmp_path: Path) -> None:
    """repeat=0、负数与 warmup 负数被明确拒绝。"""
    _seed_dataset(tmp_path)
    with pytest.raises(ValueError):
        await _run(tmp_path, warmup=0, repeat=0)
    with pytest.raises(ValueError):
        await _run(tmp_path, warmup=0, repeat=-1)
    with pytest.raises(ValueError):
        await _run(tmp_path, warmup=-1, repeat=1)


@pytest.mark.asyncio
async def test_unknown_dataset_rejected(tmp_path: Path) -> None:
    """未知 Dataset 被明确拒绝。"""
    with pytest.raises(BaselineExperimentError):
        await EvalBaselineRunner().run_dataset(
            tmp_path, "no_such_dataset", warmup=0, repeat=1, output_dir=tmp_path / "r"
        )


@pytest.mark.asyncio
async def test_empty_dataset_is_experiment_error(tmp_path: Path) -> None:
    """空 Dataset 视为实验错误，不生成基线。"""
    (tmp_path / _DATASET / "cases").mkdir(parents=True)
    with pytest.raises(BaselineExperimentError):
        await _run(tmp_path, warmup=0, repeat=1)


@pytest.mark.asyncio
async def test_trusted_result_without_trace_fails(tmp_path: Path, monkeypatch) -> None:
    """可信结果缺少 Trace 视为 Eval 契约被破坏，实验失败。"""

    async def fake_run_case(self, case):
        from dotclaw.eval.models import SCHEMA_VERSION
        from dotclaw.eval.results import EvalResult
        return EvalResult(
            schema_version=SCHEMA_VERSION,
            case_id=case.case_id,
            run_id="r1",
            passed=True,
            assertion_results=(),
            trace=None,
        )

    monkeypatch.setattr(ReexecutionRunner, "run_case", fake_run_case)
    _seed_dataset(tmp_path)
    with pytest.raises(BaselineExperimentError, match="缺少 Trace"):
        await _run(tmp_path, warmup=0, repeat=1)


@pytest.mark.asyncio
async def test_untrusted_result_fails(tmp_path: Path, monkeypatch) -> None:
    """不可信结果（fixture_configuration）使整次实验失败，不生成基线。"""

    async def fake_run_case(self, case):
        from dotclaw.eval.models import SCHEMA_VERSION
        from dotclaw.eval.results import EvalResult
        return EvalResult(
            schema_version=SCHEMA_VERSION,
            case_id=case.case_id,
            run_id=None,
            passed=False,
            assertion_results=(),
            failure_kind=EvaluationFailureKind.FIXTURE_CONFIGURATION,
            failure_detail="未匹配的 fixture",
        )

    monkeypatch.setattr(ReexecutionRunner, "run_case", fake_run_case)
    _seed_dataset(tmp_path)
    with pytest.raises(BaselineExperimentError, match="不可信结果"):
        await _run(tmp_path, warmup=0, repeat=1)


@pytest.mark.asyncio
async def test_sample_count_mismatch_fails(tmp_path: Path, monkeypatch) -> None:
    """结果数量与 Case 数不一致时实验失败。"""
    import benchmarks.eval_baseline as eb

    def fake_sample_from_result(result, *, case_id, attempt, is_warmup, wall_duration_ms, **kwargs):
        # 模拟采样记录的 case_id 与 Dataset 声明不一致（采样记录丢失）
        return make_sample(
            case_id="ghost_case",
            attempt=attempt,
            is_warmup=is_warmup,
            wall_duration_ms=wall_duration_ms,
        )

    monkeypatch.setattr(eb, "_sample_from_result", fake_sample_from_result)
    _seed_dataset(tmp_path, case_ids=("tool_success",))
    with pytest.raises(BaselineExperimentError, match="正式采样数为 0"):
        await _run(tmp_path, warmup=0, repeat=1)


@pytest.mark.asyncio
async def test_assertion_failure_still_generates_snapshot(tmp_path: Path, monkeypatch) -> None:
    """全部断言失败但 Trace 完整时仍生成快照，失败归因正确。"""

    async def fake_run_case(self, case):
        from dotclaw.eval.models import Expectation
        from dotclaw.eval.runner import EvalRunner
        failing = dataclasses.replace(case, expectations=(Expectation("run_status", "outcome", "failed"),))
        return await EvalRunner().run(failing)

    monkeypatch.setattr(ReexecutionRunner, "run_case", fake_run_case)
    _seed_dataset(tmp_path)
    snapshot = await _run(tmp_path, warmup=0, repeat=1)

    assert snapshot.global_summary.passed_count == 0
    assert snapshot.global_summary.failed_count == 4
    assert snapshot.global_summary.failure_kinds == {"assertion": 4}
    assert all(case.failure_kinds == {"assertion": 1} for case in snapshot.cases)


@pytest.mark.asyncio
async def test_existing_target_refuses_overwrite(tmp_path: Path, monkeypatch) -> None:
    """同名基线或采样文件已存在时拒绝覆盖。"""
    # 固定 snapshot-id，保证第二次运行目标文件与第一次完全相同
    monkeypatch.setattr(
        "benchmarks.eval_baseline.make_snapshot_id",
        lambda: "20260806T091530Z_b6426cc",
    )
    _seed_dataset(tmp_path)
    output_dir = tmp_path / "reports"
    await EvalBaselineRunner().run_dataset(
        tmp_path, _DATASET, warmup=0, repeat=1, output_dir=output_dir
    )
    # 第二次运行（同 output_dir、同 snapshot_id）必须失败
    with pytest.raises(FileExistsError):
        await EvalBaselineRunner().run_dataset(
            tmp_path, _DATASET, warmup=0, repeat=1, output_dir=output_dir
        )


# --------------------------------------------------------------------------- #
# 数据损坏
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_malformed_case_json_fails(tmp_path: Path) -> None:
    """Case JSON 损坏由现有 Eval loader 明确失败，不产出半成品基线。"""
    from dotclaw.eval.models import EvalCaseValidationError

    cases_dir = tmp_path / _DATASET / "cases"
    cases_dir.mkdir(parents=True)
    (cases_dir / "broken.json").write_text(json.dumps({"schema_version": "1.0"}), encoding="utf-8")
    with pytest.raises(EvalCaseValidationError):
        await _run(tmp_path, warmup=0, repeat=1)


def test_report_paths_stay_within_output_dir(tmp_path: Path) -> None:
    """Markdown 报告引用的原始证据路径不得越出输出目录。"""
    _seed_dataset(tmp_path)

    import asyncio
    snapshot = asyncio.run(_run(tmp_path, warmup=0, repeat=1))
    report = build_markdown_report(snapshot)

    output_dir = (tmp_path / "reports").resolve()
    for line in report.splitlines():
        if "（JSONL）" in line and "`" in line:
            raw = line.split("`")[1]
            resolved = (output_dir / raw).resolve()
            assert resolved.is_relative_to(output_dir), f"证据路径越界：{raw}"
    assert "samples/" in report


# --------------------------------------------------------------------------- #
# 辅助函数
# --------------------------------------------------------------------------- #


def test_make_snapshot_id_format() -> None:
    """snapshot-id 固定为 YYYYMMDDTHHMMSSZ_<short-git-commit>（UTC）。"""
    from datetime import datetime, timezone

    snapshot_id = make_snapshot_id(datetime(2026, 8, 6, 9, 15, 30, tzinfo=timezone.utc))
    assert snapshot_id.startswith("20260806T091530Z_")
    assert snapshot_id.split("_")[1] == git_short_commit()


def test_config_hash_is_stable_hex() -> None:
    """配置哈希是稳定摘要；缺失配置时返回 unknown。"""
    digest = config_hash("config.yaml", "model_router_config.yaml")
    assert len(digest) == 16
    assert digest == config_hash("config.yaml", "model_router_config.yaml")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def test_cli_writes_all_artifacts(tmp_path: Path, monkeypatch) -> None:
    """CLI 以最小采样写出 JSONL、JSON 快照与 Markdown 报告。"""
    from benchmarks.eval_baseline import main

    _seed_dataset(tmp_path)
    output = tmp_path / "cli_reports"
    baseline = tmp_path / "cli_baselines"
    code = main([
        "--dataset-root", str(tmp_path),
        "--dataset", _DATASET,
        "--warmup", "0",
        "--repeat", "1",
        "--output", str(output),
        "--save-baseline", str(baseline),
    ])
    assert code == 0

    files = list(output.iterdir())
    jsonl_dirs = list(output.glob("samples/*.jsonl"))
    assert jsonl_dirs and jsonl_dirs[0].exists()
    json_files = [f for f in files if f.suffix == ".json"]
    md_files = [f for f in files if f.suffix == ".md"]
    assert len(json_files) == 1
    assert len(md_files) == 1
    # 基线目录
    baseline_jsons = list(baseline.glob("*.json"))
    baseline_jsonls = list(baseline.glob("samples/*.jsonl"))
    assert len(baseline_jsons) == 1
    assert len(baseline_jsonls) == 1


def test_cli_rejects_bad_repeat(tmp_path: Path) -> None:
    """CLI 对 repeat=0 明确拒绝并退出非零。"""
    from benchmarks.eval_baseline import main

    with pytest.raises(SystemExit) as exc:
        main(["--dataset-root", str(tmp_path), "--warmup", "0", "--repeat", "0"])
    assert exc.value.code == 2


def test_cli_unknown_dataset_fails(tmp_path: Path) -> None:
    """CLI 遇到未知 Dataset 返回非零退出码。"""
    from benchmarks.eval_baseline import main

    code = main(["--dataset-root", str(tmp_path), "--dataset", "nope", "--warmup", "0", "--repeat", "1"])
    assert code == 1
