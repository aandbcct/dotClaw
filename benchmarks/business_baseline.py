"""PR8 代表性业务任务基线 CLI；正式采样必须由显式命令启动。"""

from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
import platform
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import Mapping, Sequence

from dotclaw.eval.dataset import load_case
from dotclaw.eval.reexecution import ReexecutionRunner

from .business_judge import JudgePort, JudgeProtocolError, JudgeSpec, parse_verdict, prompt_hash, render_prompt
from .business_workflows import run_compressed_history_continuation, run_preference_aware_followup
from .eval_baseline import _sample_from_result, compute_fixture_fingerprint, git_full_commit, git_short_commit, config_hash
from .eval_baseline_models import BenchmarkSample
from .eval_baseline_stats import build_snapshot

_FAILURES = {"runtime_failure", "fixture_or_trace_error", "assertion_failure", "judge_quality_failure", "judge_error"}


class BusinessDatasetError(ValueError):
    """业务任务集或跨样本资格不满足冻结契约。"""


def load_business_documents(root: Path, dataset: str) -> tuple[dict[str, Mapping[str, object]], dict[str, Mapping[str, object]]]:
    """读取八份标准任务与两份工作流；所有任务必须有版本、类别和固定来源。"""
    base = root / dataset
    cases: dict[str, Mapping[str, object]] = {}
    workflows: dict[str, Mapping[str, object]] = {}
    for directory, target in ((base / "cases", cases), (base / "workflows", workflows)):
        for path in sorted(directory.glob("*.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise BusinessDatasetError(f"{path} 必须是 JSON 对象")
            task_id = data.get("task_id")
            required = {"task_id", "version", "category", "kind", "fixture_source", "expected_delivery", "tags"}
            if not isinstance(task_id, str) or not task_id or required - set(data):
                raise BusinessDatasetError(f"{path} 缺少业务任务必需字段")
            if task_id in cases or task_id in workflows:
                raise BusinessDatasetError(f"重复任务 ID：{task_id}")
            target[task_id] = data
    if len(cases) != 8 or len(workflows) != 2:
        raise BusinessDatasetError("runtime_core_v2 必须恰有 8 个 Case 与 2 个 Session 工作流")
    return cases, workflows


def load_judge_spec(root: Path, dataset: str, task_id: str) -> JudgeSpec:
    """读取任务对应的冻结裁判规范。"""
    path = root / dataset / "judge_specs" / f"{task_id}.json"
    if not path.is_file():
        raise BusinessDatasetError(f"[EXT] 任务缺少裁判规范：{task_id}")
    return JudgeSpec.from_dict(json.loads(path.read_text(encoding="utf-8")))


def _attribution(sample: BenchmarkSample) -> str | None:
    """按计划规定的执行链优先级生成唯一主失败归因。"""
    if sample.passed:
        return None
    if not sample.trace_available or sample.failure_kind in {"runtime", "unavailable"}:
        return "runtime_failure"
    if sample.failure_kind in {"fixture_configuration", "trace_reconstruction"}:
        return "fixture_or_trace_error"
    return "assertion_failure"


async def judge_deterministic_candidate(sample: BenchmarkSample, candidate: str, spec: JudgeSpec, judge: JudgePort) -> BenchmarkSample:
    """仅对确定性链路通过的 EXT 样本调用一次裁判，并保存脱敏结论摘要。"""
    if sample.execution_mode != "ext" or sample.deterministic_passed is not True:
        return sample
    try:
        verdict = parse_verdict(await judge.judge(render_prompt(spec, candidate)), spec)
    except (JudgeProtocolError, TimeoutError):
        return replace(sample, judge_spec_version=spec.version, judge_prompt_hash=prompt_hash(spec), judge_verdict="error", failure_attribution="judge_error", passed=False)
    if verdict.verdict == "fail":
        return replace(sample, judge_spec_version=spec.version, judge_prompt_hash=prompt_hash(spec), judge_verdict="fail", judge_criteria=verdict.criteria, failure_attribution="judge_quality_failure", passed=False)
    return replace(sample, judge_spec_version=spec.version, judge_prompt_hash=prompt_hash(spec), judge_verdict="pass", judge_criteria=verdict.criteria)


async def run_fixture_dataset(root: Path, dataset: str, *, warmup: int, repeat: int, output: Path, baseline: Path | None = None) -> object:
    """执行固定 Fixture 任务；不触发真实模型、工具、审批或委派。"""
    cases, workflows = load_business_documents(root, dataset)
    # 标准 Case 通过既有 Runner 保留完整 Fixture 消费与 Trace 语义。
    source_root = root
    source_dataset = "runtime_core_v1"
    raw_samples: list[BenchmarkSample] = []
    for task_id, doc in cases.items():
        source = str(doc["fixture_source"])
        case = load_case(source_root, source_dataset, source)
        for index in range(warmup + repeat):
            # 直接调用公开重执行器，保证 Fixture/Trace 校验与 PR1 同口径。
            started = time.perf_counter(); evaluated = await ReexecutionRunner().run_case(case); elapsed = (time.perf_counter() - started) * 1000
            sample = _sample_from_result(evaluated, dataset=dataset, case_id=task_id, scenario_id=task_id, fixture_fingerprint=compute_fixture_fingerprint(case), attempt=index if index < warmup else index - warmup, is_warmup=index < warmup, wall_duration_ms=elapsed, git_commit=git_short_commit(), source_commit=git_full_commit(), python_version=sys.version.split()[0], platform_name=platform.platform(), config_hash_value=config_hash())
            raw_samples.append(replace(sample, task_category=str(doc["category"]), task_kind="standard_case", execution_mode="fixture", deterministic_passed=sample.passed, failure_attribution=_attribution(sample), llm_call_count=int(sample.run_statistics.get("llm_call_count", 0)), tool_call_count=int(sample.run_statistics.get("tool_call_count", 0)), dataset_version="2"))
    # 工作流在临时根运行真实 Session 与 create_run_request() 路径。
    for task_id, doc in workflows.items():
        action = run_preference_aware_followup if task_id == "preference_aware_followup" else run_compressed_history_continuation
        for index in range(warmup + repeat):
            with tempfile.TemporaryDirectory(prefix="dotclaw-pr8-") as temp:
                result = await action(Path(temp))
            raw_samples.append(BenchmarkSample(dataset=dataset, case_id=task_id, attempt=index if index < warmup else index - warmup, is_warmup=index < warmup, git_commit=git_short_commit(), python_version="fixture", platform="fixture", config_hash=config_hash(), eval_schema_version="1.0", passed=result.passed, failure_kind=None if result.passed else "workflow", assertions_passed=1 if result.passed else 0, assertions_total=1, trace_available=result.run_id is not None, wall_duration_ms=0.0, run_id=result.run_id, task_category=str(doc["category"]), task_kind="session_workflow", execution_mode="fixture", deterministic_passed=result.passed, failure_attribution=None if result.passed else "assertion_failure", dataset_version="2", workflow_version=str(doc["version"])))
    formal = [sample for sample in raw_samples if not sample.is_warmup]
    expected = 10 * repeat
    if len(formal) != expected:
        raise BusinessDatasetError(f"正式样本数 {len(formal)} 与 10*repeat={expected} 不一致")
    snapshot = build_snapshot(snapshot_id="development", generated_at="development", git_commit=git_short_commit(), dataset=dataset, environment={"config_hash": config_hash()}, warmup=warmup, repeat=repeat, samples=raw_samples, samples_path="samples/development.jsonl", samples_content_summary={"line_count": len(raw_samples)})
    output.mkdir(parents=True, exist_ok=True)
    (output / "fixture-summary.md").write_text(f"# PR8 Fixture 开发运行\n\n正式样本：{len(formal)}\n", encoding="utf-8")
    return snapshot


def main(argv: Sequence[str] | None = None) -> int:
    """只提供显式 CLI；EXT 需要注入 Provider/Judge，避免默认真实 API 调用。"""
    parser = argparse.ArgumentParser(description="PR8 代表性业务任务基线")
    parser.add_argument("--dataset-root", type=Path, default=Path("benchmarks/datasets")); parser.add_argument("--dataset", default="runtime_core_v2")
    parser.add_argument("--mode", choices=("fixture", "ext"), required=True); parser.add_argument("--warmup", type=int, default=5); parser.add_argument("--repeat", type=int, default=30); parser.add_argument("--output", type=Path, required=True); parser.add_argument("--save-baseline", type=Path)
    args = parser.parse_args(argv)
    if args.mode == "ext":
        raise BusinessDatasetError("EXT 必须由集成方显式注入真实 LLM 与 Judge；开发期不启动真实 API")
    asyncio.run(run_fixture_dataset(args.dataset_root, args.dataset, warmup=args.warmup, repeat=args.repeat, output=args.output, baseline=args.save_baseline))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
