"""PR8 代表性业务任务基线 CLI；正式采样必须由显式命令启动。"""

from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
import platform
import sys
import time
from datetime import datetime, timezone
from dataclasses import replace
from pathlib import Path
from typing import Mapping, Sequence

from dotclaw.eval.dataset import load_case
from dotclaw.eval.environment import EvalDependencies
from dotclaw.eval.models import LLMFixture
from dotclaw.eval.reexecution import ReexecutionRunner

from .business_judge import JudgePort, JudgeProtocolError, JudgeSpec, LLMProxyJudge, parse_verdict, prompt_hash, render_prompt
from .business_workflows import run_compressed_history_continuation, run_preference_aware_followup
from .business_report import render_partial_report, summarize_business_samples, write_business_reports
from .eval_baseline import _sample_from_result, compute_fixture_fingerprint, git_full_commit, git_short_commit, config_hash, make_snapshot_id, write_jsonl
from .eval_baseline_models import BenchmarkSample, BenchmarkSnapshot
from .eval_baseline_stats import build_snapshot

_FAILURES = {"runtime_failure", "fixture_or_trace_error", "assertion_failure", "judge_quality_failure", "judge_error"}


class BusinessDatasetError(ValueError):
    """业务任务集或跨样本资格不满足冻结契约。"""


def load_business_documents(root: Path, dataset: str) -> tuple[dict[str, Mapping[str, object]], dict[str, Mapping[str, object]]]:
    """读取八份可执行 Case 与两份工作流；业务元数据与 Case 同文件冻结。"""
    base = root / dataset
    cases: dict[str, Mapping[str, object]] = {}
    workflows: dict[str, Mapping[str, object]] = {}
    for directory, target in ((base / "cases", cases), (base / "workflows", workflows)):
        for path in sorted(directory.glob("*.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise BusinessDatasetError(f"{path} 必须是 JSON 对象")
            task_id = data.get("task_id")
            required = {"task_id", "version", "category", "kind", "expected_delivery", "tags", "delivery_markers"}
            if not isinstance(task_id, str) or not task_id or required - set(data):
                raise BusinessDatasetError(f"{path} 缺少业务任务必需字段")
            if target is cases and data.get("case_id") != task_id:
                raise BusinessDatasetError(f"{path} 的 task_id 必须与可执行 case_id 一致")
            if task_id in cases or task_id in workflows:
                raise BusinessDatasetError(f"重复任务 ID：{task_id}")
            target[task_id] = data
    if len(cases) != 8 or len(workflows) != 2:
        raise BusinessDatasetError("runtime_core_v2 必须恰有 8 个 Case 与 2 个 Session 工作流")
    return cases, workflows


def _business_delivery_passed(result: object, document: Mapping[str, object]) -> bool:
    """验证任务自身的冻结交付标记，避免只把底层 Eval 断言当作业务完成。"""
    markers = document.get("delivery_markers")
    if not isinstance(markers, list) or not markers or not all(isinstance(item, str) and item for item in markers):
        raise BusinessDatasetError("每个标准业务任务必须定义 delivery_markers")
    trace = getattr(result, "trace", None)
    output = "\n".join(str(getattr(message, "content", "")) for message in getattr(trace, "messages", ())).lower()
    outcome = str(getattr(getattr(trace, "run", None), "outcome", "")).lower()
    return all(marker.lower() in output or marker.lower() == outcome for marker in markers)


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


def _candidate_output(result: object) -> str:
    """从已重建 Trace 取最终助手交付；缺失时视为不可评分。"""
    trace = getattr(result, "trace", None)
    messages = getattr(trace, "messages", ()) if trace is not None else ()
    for message in reversed(messages):
        if getattr(getattr(message, "role", None), "value", "") == "assistant" and getattr(message, "content", ""):
            return str(message.content)
    raise BusinessDatasetError("EXT 样本没有可评分的最终助手输出")


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


async def run_ext_dataset(root: Path, dataset: str, *, warmup: int, repeat: int, output: Path, baseline: Path | None, dependencies: EvalDependencies, judge: JudgePort, provider: str, model: str, temperature: float, judge_provider: str, judge_model: str) -> BenchmarkSnapshot:
    """执行六个 EXT 任务：只允许真实 LLM 回退，所有副作用仍由 Case Fixture 覆盖。"""
    if not provider or not model or not judge_provider or not judge_model:
        raise BusinessDatasetError("EXT 必须显式记录 Provider、模型及 Judge 条件")
    if dependencies.llm_port is None:
        raise BusinessDatasetError("EXT 必须注入真实 LLMPort，不能回退到脚本化响应")
    cases, workflows = load_business_documents(root, dataset)
    ext_ids = {task_id for task_id, doc in cases.items() if "ext" in doc["tags"]} | {task_id for task_id, doc in workflows.items() if "ext" in doc["tags"]}
    if len(ext_ids) != 6:
        raise BusinessDatasetError("runtime_core_v2 必须恰有六个 EXT 任务")
    samples: list[BenchmarkSample] = []
    runner = ReexecutionRunner(dependencies)
    for task_id in sorted(ext_ids):
        if task_id in workflows:
            doc = workflows[task_id]
            spec = load_judge_spec(root, dataset, task_id)
            for index in range(warmup + repeat):
                started = time.perf_counter()
                with tempfile.TemporaryDirectory(prefix="dotclaw-pr8-ext-") as temporary_root:
                    result = await run_preference_aware_followup(Path(temporary_root), dependencies.llm_port)
                sample = BenchmarkSample(dataset=dataset, case_id=task_id, attempt=index if index < warmup else index - warmup, is_warmup=index < warmup, git_commit=git_short_commit(), python_version=sys.version.split()[0], platform=platform.platform(), config_hash=config_hash(), eval_schema_version="1.0", passed=result.passed, failure_kind=None if result.passed else "workflow", assertions_passed=1 if result.passed else 0, assertions_total=1, trace_available=result.run_id is not None, wall_duration_ms=(time.perf_counter() - started) * 1000, run_id=result.run_id, task_category=str(doc["category"]), task_kind="session_workflow", execution_mode="ext", deterministic_passed=result.passed, failure_attribution=None if result.passed else "assertion_failure", provider=provider, model=model, temperature=temperature, judge_provider=judge_provider, judge_model=judge_model, dataset_version="2", workflow_version=str(doc["version"]))
                if not sample.is_warmup and sample.deterministic_passed:
                    sample = await judge_deterministic_candidate(sample, result.final_output, spec, judge)
                samples.append(sample)
            continue
        doc = cases[task_id]
        source_case = load_case(root, dataset, task_id)
        # 清空脚本化 LLM 响应，使 REEXECUTION 仅在 LLM 槽位回退到注入真实端口；工具等仍由 Fixture 拦截。
        ext_case = replace(source_case, llm_fixture=LLMFixture(source_case.llm_fixture.fixture_id, ()))
        spec = load_judge_spec(root, dataset, task_id)
        for index in range(warmup + repeat):
            started = time.perf_counter()
            result = await runner.run_case(ext_case)
            sample = _sample_from_result(result, dataset=dataset, case_id=task_id, scenario_id=task_id, fixture_fingerprint=compute_fixture_fingerprint(ext_case), attempt=index if index < warmup else index - warmup, is_warmup=index < warmup, wall_duration_ms=(time.perf_counter() - started) * 1000, git_commit=git_short_commit(), source_commit=git_full_commit(), python_version=sys.version.split()[0], platform_name=platform.platform(), config_hash_value=config_hash())
            deterministic = sample.passed and _business_delivery_passed(result, doc)
            sample = replace(sample, passed=deterministic, failure_kind=sample.failure_kind if sample.failure_kind is not None else (None if deterministic else "business_delivery"), task_category=str(doc["category"]), task_kind="standard_case", execution_mode="ext", deterministic_passed=deterministic, failure_attribution=_attribution(replace(sample, passed=deterministic)), llm_call_count=int(sample.run_statistics.get("llm_call_count", 0)), tool_call_count=int(sample.run_statistics.get("tool_call_count", 0)), provider=provider, model=model, temperature=temperature, judge_provider=judge_provider, judge_model=judge_model, dataset_version="2")
            if not sample.is_warmup and sample.deterministic_passed:
                sample = await judge_deterministic_candidate(sample, _candidate_output(result), spec, judge)
            samples.append(sample)
    if len([item for item in samples if not item.is_warmup]) != 6 * repeat:
        raise BusinessDatasetError("EXT 正式样本数与 6*repeat 不一致")
    snapshot_id = make_snapshot_id()
    snapshot = build_snapshot(snapshot_id=snapshot_id, generated_at=datetime.now(timezone.utc).isoformat(), git_commit=git_short_commit(), dataset=dataset, environment={"config_hash": config_hash(), "provider": provider, "model": model, "judge_provider": judge_provider, "judge_model": judge_model}, warmup=warmup, repeat=repeat, samples=samples, samples_path=f"samples/ext-{snapshot_id}.jsonl", samples_content_summary={"line_count": len(samples)})
    output.mkdir(parents=True, exist_ok=True)
    _write_ext_artifacts(samples, snapshot, output, baseline)
    return snapshot


def _write_fixture_artifacts(samples: Sequence[BenchmarkSample], snapshot: BenchmarkSnapshot, output: Path, baseline: Path | None) -> None:
    """写出不可覆盖的 JSONL、快照、配置与分区报告；可选复制为待审核基线。"""
    sample_path = output / snapshot.samples_path
    snapshot_path = output / f"{snapshot.snapshot_id}.json"
    targets = [sample_path, snapshot_path, output / "fixture-summary.md", output / "business-config.json"]
    if any(item.exists() for item in targets):
        raise FileExistsError("PR8 运行工件已存在，拒绝覆盖")
    write_jsonl(sample_path, samples)
    snapshot_path.write_text(json.dumps(snapshot.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    summary = summarize_business_samples(samples, "fixture")
    (output / "fixture-summary.md").write_text(render_partial_report(summary), encoding="utf-8")
    (output / "failure-attribution.md").write_text(json.dumps(summary["failure_attribution"], ensure_ascii=False, indent=2), encoding="utf-8")
    write_business_reports(output, summary)
    (output / "business-config.json").write_text(json.dumps({"dataset": snapshot.dataset, "warmup": snapshot.warmup, "repeat": snapshot.repeat, "config_hash": snapshot.environment.get("config_hash"), "mode": "fixture"}, ensure_ascii=False, indent=2), encoding="utf-8")
    if baseline is not None:
        baseline_samples, baseline_snapshot = baseline / snapshot.samples_path, baseline / f"{snapshot.snapshot_id}.json"
        if baseline_samples.exists() or baseline_snapshot.exists():
            raise FileExistsError("PR8 基线工件已存在，拒绝覆盖")
        write_jsonl(baseline_samples, samples)
        baseline_snapshot.parent.mkdir(parents=True, exist_ok=True)
        baseline_snapshot.write_text(json.dumps(snapshot.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")


def _write_ext_artifacts(samples: Sequence[BenchmarkSample], snapshot: BenchmarkSnapshot, output: Path, baseline: Path | None) -> None:
    """写出 EXT 原始样本、质量报告与可选待审核基线，不与 Fixture 混合。"""
    sample_path, snapshot_path = output / snapshot.samples_path, output / f"{snapshot.snapshot_id}.json"
    targets = [sample_path, snapshot_path, output / "ext-quality.md", output / "business-config.json"]
    if any(item.exists() for item in targets):
        raise FileExistsError("PR8 EXT 工件已存在，拒绝覆盖")
    write_jsonl(sample_path, samples)
    snapshot_path.write_text(json.dumps(snapshot.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    summary = summarize_business_samples(samples, "ext")
    (output / "ext-quality.md").write_text(
        "# PR8 EXT 质量开发报告\n\n"
        f"正式样本：{summary['sample_count']}；进入 Judge：{summary['entered_judge']}；质量通过：{summary['quality_passed']}。\n",
        encoding="utf-8",
    )
    write_business_reports(output, {"status": "fixture_snapshot_required"}, summary)
    (output / "business-config.json").write_text(json.dumps(dict(snapshot.environment), ensure_ascii=False, indent=2), encoding="utf-8")
    if baseline is not None:
        baseline_samples, baseline_snapshot = baseline / snapshot.samples_path, baseline / f"{snapshot.snapshot_id}.json"
        if baseline_samples.exists() or baseline_snapshot.exists():
            raise FileExistsError("PR8 EXT 基线工件已存在，拒绝覆盖")
        write_jsonl(baseline_samples, samples); baseline_snapshot.parent.mkdir(parents=True, exist_ok=True)
        baseline_snapshot.write_text(json.dumps(snapshot.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")


async def run_fixture_dataset(root: Path, dataset: str, *, warmup: int, repeat: int, output: Path, baseline: Path | None = None) -> BenchmarkSnapshot:
    """执行固定 Fixture 任务；不触发真实模型、工具、审批或委派。"""
    cases, workflows = load_business_documents(root, dataset)
    # 标准 Case 直接从 v2 加载，确保业务元数据、Fixture 与确定性断言属于同一条链路。
    raw_samples: list[BenchmarkSample] = []
    for task_id, doc in cases.items():
        case = load_case(root, dataset, task_id)
        for index in range(warmup + repeat):
            # 直接调用公开重执行器，保证 Fixture/Trace 校验与 PR1 同口径。
            started = time.perf_counter(); evaluated = await ReexecutionRunner().run_case(case); elapsed = (time.perf_counter() - started) * 1000
            sample = _sample_from_result(evaluated, dataset=dataset, case_id=task_id, scenario_id=task_id, fixture_fingerprint=compute_fixture_fingerprint(case), attempt=index if index < warmup else index - warmup, is_warmup=index < warmup, wall_duration_ms=elapsed, git_commit=git_short_commit(), source_commit=git_full_commit(), python_version=sys.version.split()[0], platform_name=platform.platform(), config_hash_value=config_hash())
            deterministic = sample.passed and _business_delivery_passed(evaluated, doc)
            raw_samples.append(replace(sample, passed=deterministic, failure_kind=sample.failure_kind if sample.failure_kind is not None else (None if deterministic else "business_delivery"), task_category=str(doc["category"]), task_kind="standard_case", execution_mode="fixture", deterministic_passed=deterministic, failure_attribution=_attribution(replace(sample, passed=deterministic)), llm_call_count=int(sample.run_statistics.get("llm_call_count", 0)), tool_call_count=int(sample.run_statistics.get("tool_call_count", 0)), dataset_version="2"))
    # 工作流在临时根运行真实 Session 与 create_run_request() 路径。
    for task_id, doc in workflows.items():
        action = run_preference_aware_followup if task_id == "preference_aware_followup" else run_compressed_history_continuation
        for index in range(warmup + repeat):
            with tempfile.TemporaryDirectory(prefix="dotclaw-pr8-") as temp:
                result = await action(Path(temp))
            markers = doc.get("delivery_markers")
            if not isinstance(markers, list) or not all(isinstance(item, str) for item in markers):
                raise BusinessDatasetError("Session 工作流必须定义 delivery_markers")
            deterministic = result.passed and all(item in result.final_output for item in markers)
            raw_samples.append(BenchmarkSample(dataset=dataset, case_id=task_id, attempt=index if index < warmup else index - warmup, is_warmup=index < warmup, git_commit=git_short_commit(), python_version="fixture", platform="fixture", config_hash=config_hash(), eval_schema_version="1.0", passed=deterministic, failure_kind=None if deterministic else "workflow_delivery", assertions_passed=1 if deterministic else 0, assertions_total=1, trace_available=result.run_id is not None, wall_duration_ms=0.0, run_id=result.run_id, task_category=str(doc["category"]), task_kind="session_workflow", execution_mode="fixture", deterministic_passed=deterministic, failure_attribution=None if deterministic else "assertion_failure", dataset_version="2", workflow_version=str(doc["version"])))
    formal = [sample for sample in raw_samples if not sample.is_warmup]
    expected = 10 * repeat
    if len(formal) != expected:
        raise BusinessDatasetError(f"正式样本数 {len(formal)} 与 10*repeat={expected} 不一致")
    snapshot_id = make_snapshot_id()
    snapshot = build_snapshot(snapshot_id=snapshot_id, generated_at=datetime.now(timezone.utc).isoformat(), git_commit=git_short_commit(), dataset=dataset, environment={"config_hash": config_hash(), "python_version": sys.version.split()[0], "platform": platform.platform()}, warmup=warmup, repeat=repeat, samples=raw_samples, samples_path=f"samples/fixture-{snapshot_id}.jsonl", samples_content_summary={"line_count": len(raw_samples)})
    output.mkdir(parents=True, exist_ok=True)
    _write_fixture_artifacts(raw_samples, snapshot, output, baseline)
    return snapshot


def main(argv: Sequence[str] | None = None) -> int:
    """只提供显式 CLI；EXT 需要注入 Provider/Judge，避免默认真实 API 调用。"""
    parser = argparse.ArgumentParser(description="PR8 代表性业务任务基线")
    parser.add_argument("--dataset-root", type=Path, default=Path("benchmarks/datasets")); parser.add_argument("--dataset", default="runtime_core_v2")
    parser.add_argument("--mode", choices=("fixture", "ext"), required=True); parser.add_argument("--warmup", type=int, default=5); parser.add_argument("--repeat", type=int, default=30); parser.add_argument("--output", type=Path, required=True); parser.add_argument("--save-baseline", type=Path)
    parser.add_argument("--provider"); parser.add_argument("--model"); parser.add_argument("--temperature", type=float, default=0.0); parser.add_argument("--judge-provider"); parser.add_argument("--judge-model")
    args = parser.parse_args(argv)
    if args.mode == "ext":
        if not all((args.provider, args.model, args.judge_provider, args.judge_model)):
            raise BusinessDatasetError("EXT CLI 必须提供 provider、model、judge-provider、judge-model")
        from dotclaw.bootstrap._host_components import _build_llm
        from dotclaw.config.settings import load_config
        from dotclaw.runtime.adapters import LLMProxyAdapter
        proxy = _build_llm(load_config(), Path.cwd())
        dependencies = EvalDependencies(llm_port=LLMProxyAdapter(proxy))
        asyncio.run(run_ext_dataset(args.dataset_root, args.dataset, warmup=args.warmup, repeat=args.repeat, output=args.output, baseline=args.save_baseline, dependencies=dependencies, judge=LLMProxyJudge(proxy, args.judge_model), provider=args.provider, model=args.model, temperature=args.temperature, judge_provider=args.judge_provider, judge_model=args.judge_model))
        return 0
    asyncio.run(run_fixture_dataset(args.dataset_root, args.dataset, warmup=args.warmup, repeat=args.repeat, output=args.output, baseline=args.save_baseline))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
