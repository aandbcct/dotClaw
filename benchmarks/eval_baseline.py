"""PR1 当前 Eval 基线 CLI 与编排。

链路：``benchmarks/datasets/runtime_core_v1/cases/*.json`` → ``ReexecutionRunner``
→ ``EvalResult + RunTrace`` → ``BenchmarkSample``（JSONL）→ ``BenchmarkSnapshot``
（JSON + Markdown）。

本模块只写 ``output_dir`` 与可选 ``baseline_dir``，不写 Dataset、Session、生产目录
或 Runtime 事实。CLI 默认只使用隔离 Fixture；warmup 与 repeat 参数严格校验，
Dataset 为空、结果数量与 Case 数不一致、无 Trace 的可信结果均视为实验错误，
不生成可用基线。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import platform
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Sequence

from dotclaw.eval.dataset import load_cases
from dotclaw.eval.environment import EvalDependencies
from dotclaw.eval.models import EVAL_SCHEMA_VERSION
from dotclaw.eval.reexecution import ReexecutionRunner
from dotclaw.eval.results import EvalResult
from dotclaw.runtime.domain.facts import RunStatistics

from .eval_baseline_models import BenchmarkSample, BenchmarkSnapshot
from .eval_baseline_stats import build_snapshot

# 可信结果的失败分类：None（全部通过）与 assertion（已可信执行但行为不符）。
# 其余分类（runtime / trace_reconstruction / fixture_configuration）表示执行本身
# 不可信，任何一次出现都使整次实验不生成基线。
_TRUSTED_FAILURE_KINDS: frozenset[str] = frozenset({None, "assertion"})


class BaselineExperimentError(RuntimeError):
    """实验错误：无法生成可用基线。"""


def git_short_commit() -> str:
    """返回当前 HEAD 的短提交号；非 Git 仓库或读取失败时返回 ``unknown``。"""
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    if proc.returncode != 0:
        return "unknown"
    return proc.stdout.strip() or "unknown"


def config_hash(config_path: str = "config.yaml", router_config_path: str = "model_router_config.yaml") -> str:
    """计算配置文件的内容摘要；任一文件缺失时返回 ``unknown``。"""
    digest = hashlib.sha256()
    try:
        for path in (config_path, router_config_path):
            digest.update(Path(path).read_bytes())
    except OSError:
        return "unknown"
    return digest.hexdigest()[:16]


def make_snapshot_id(now: datetime | None = None) -> str:
    """构造快照标识，固定为 ``YYYYMMDDTHHMMSSZ_<short-git-commit>``（UTC）。"""
    moment: datetime = now or datetime.now(timezone.utc)
    stamp: str = moment.strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}_{git_short_commit()}"


def _run_statistics_view(statistics: RunStatistics) -> Mapping[str, object]:
    """RunStatistics 的只读视图。

    隔离 Fixture 下脚本化响应不产生真实时延与 token 计数，0 表示缺少权威事实，
    必须序列化为 ``None`` 而非猜测为 0；调用计数由 Fixture 消费产生，保留原值。
    """
    return {
        "duration_ms": None if statistics.duration_ms == 0 else statistics.duration_ms,
        "llm_call_count": statistics.llm_call_count,
        "tool_call_count": statistics.tool_call_count,
        "tokens_in": None if statistics.tokens_in == 0 else statistics.tokens_in,
        "tokens_out": None if statistics.tokens_out == 0 else statistics.tokens_out,
    }


def _sample_from_result(
    result: EvalResult,
    *,
    dataset: str,
    case_id: str,
    attempt: int,
    is_warmup: bool,
    wall_duration_ms: float,
    git_commit: str,
    python_version: str,
    platform_name: str,
    config_hash_value: str,
) -> BenchmarkSample:
    """把一次 ``run_case()`` 结果转为一个 BenchmarkSample。

    只提取 EvalResult / RunTrace 的派生视图，不内联正文或敏感内容。
    """
    trace = result.trace
    assertion_results = result.assertion_results
    return BenchmarkSample(
        dataset=dataset,
        case_id=case_id,
        attempt=attempt,
        is_warmup=is_warmup,
        git_commit=git_commit,
        python_version=python_version,
        platform=platform_name,
        config_hash=config_hash_value,
        eval_schema_version=EVAL_SCHEMA_VERSION,
        passed=result.passed,
        failure_kind=None if result.failure_kind is None else result.failure_kind.value,
        assertions_passed=sum(1 for item in assertion_results if item.passed),
        assertions_total=len(assertion_results),
        trace_available=trace is not None,
        wall_duration_ms=wall_duration_ms,
        run_id=result.run_id,
        trace_metrics=dict(trace.metrics.to_dict()) if trace is not None else {},
        run_statistics=_run_statistics_view(trace.run.statistics) if trace is not None else {},
        trace_source=dict(trace.source.to_dict()) if trace is not None else None,
    )


def write_jsonl(path: Path, samples: Sequence[BenchmarkSample]) -> None:
    """逐条写出 JSONL；warmup 与正式采样均写出并以 ``is_warmup`` 区分。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for sample in samples:
            fh.write(json.dumps(sample.to_dict(), ensure_ascii=False) + "\n")


def _ensure_not_exists(paths: Sequence[Path | None]) -> None:
    """任一目标文件已存在即失败，不自动改名或覆盖。"""
    existing: list[str] = [str(path) for path in paths if path is not None and path.exists()]
    if existing:
        raise FileExistsError(f"目标文件已存在，拒绝覆盖：{', '.join(existing)}")


def build_markdown_report(snapshot: BenchmarkSnapshot) -> str:
    """生成 Markdown 汇总报告。

    证据路径只引用相对路径字符串（``samples_path`` 等），保证报告引用的
    原始证据不越出输出目录。
    """
    env: Mapping[str, str] = snapshot.environment
    lines: list[str] = [
        f"# dotClaw Eval 基线快照 {snapshot.snapshot_id}",
        "",
        f"> 生成时间：{snapshot.generated_at}",
        f"> Git：`{snapshot.git_commit}`",
        f"> Dataset：`{snapshot.dataset}`",
        f"> Warmup：{snapshot.warmup} | Repeat：{snapshot.repeat}",
        f"> Python：{env.get('python_version', '')} | 平台：{env.get('platform', '')}",
        f"> 配置哈希：`{env.get('config_hash', '')}` | Eval schema：{env.get('eval_schema_version', '')}",
        "",
        "## 全局汇总",
        "",
        "| 样本数 | 通过 | 失败 | 成功率 | LLM 调用 | Tool 调用 | Trace 完整 | Trace 缺失 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    g = snapshot.global_summary
    lines.append(
        f"| {g.sample_count} | {g.passed_count} | {g.failed_count} | {g.success_rate:.2%} "
        f"| {g.llm_call_count_total} | {g.tool_call_count_total} "
        f"| {g.trace_available_count} | {g.trace_missing_count} |"
    )
    lines.extend(["", "## 失败归因", "", "| 分类 | 次数 |", "|---|---|"])
    if g.failure_kinds:
        for kind, count in sorted(g.failure_kinds.items()):
            lines.append(f"| `{kind}` | {count} |")
    else:
        lines.append("| — | 0 |")

    lines.extend(
        [
            "",
            "## 各 Case 汇总",
            "",
            "| Case | 样本数 | 通过 | 失败 | 成功率 | P50(ms) | P95(ms) | P99(ms) | Max(ms) | LLM 调用 | Tool 调用 |",
            "|---|---|---|---|---|---|---|---|---|---|---|",
        ]
    )
    for case in snapshot.cases:
        w = case.wall_duration_ms
        lines.append(
            f"| `{case.case_id}` | {case.sample_count} | {case.passed_count} | {case.failed_count} "
            f"| {case.success_rate:.2%} | {w.p50_ms:.1f} | {w.p95_ms:.1f} | {w.p99_ms:.1f} | {w.max_ms:.1f} "
            f"| {case.llm_call_count_total} | {case.tool_call_count_total} |"
        )

    lines.extend(
        [
            "",
            "## Trace 关键路径（P50, ms）",
            "",
            "| Case | LLM | Tool | Approval 等待 | 关键路径 |",
            "|---|---|---|---|---|",
        ]
    )
    for case in snapshot.cases:
        tm = case.trace_metrics_ms
        fmt = lambda key: f"{tm[key].p50_ms:.1f}" if key in tm else "—"
        lines.append(
            f"| `{case.case_id}` | {fmt('llm_duration_ms')} | {fmt('tool_duration_ms')} "
            f"| {fmt('approval_wait_ms')} | {fmt('critical_path_ms')} |"
        )

    lines.extend(
        [
            "",
            "## 边界说明",
            "",
            "- 通过率是隔离 Fixture 下的 Eval 语义通过率，不等同于真实模型线上成功率。",
            "- `wall_duration_ms` 是跨提交性能比较的端到端口径；Trace 关键路径用于解释内部耗时构成，两者不可互相替代。",
            "- P50/P95/P99 只在同机、同 Python、同 Dataset、同配置、同 repeat 下可用于后续提交趋势比较。",
            "- `RunStatistics` 中 Fixture 未产生的 token / 时延以 `null` 记录，不得猜测为 0。",
            "- 快照不是 `EvalResult` / `RegressionReport` / Runtime 事实的替代品，不进入 CI Gate。",
            "",
            "## 原始证据",
            "",
            f"- 采样记录（JSONL）：`{snapshot.samples_path}`",
            f"- 快照（JSON）：`{snapshot.snapshot_id}.json`",
            f"- 记录行数：{snapshot.samples_content_summary.get('line_count', '—')} 行，"
            f"{snapshot.samples_content_summary.get('byte_count', '—')} 字节",
        ]
    )
    return "\n".join(lines)


class EvalBaselineRunner:
    """当前 Eval 基线编排器：加载 Dataset、重复采样、写出 JSONL / JSON / Markdown。

    只由 CLI 与测试调用。每个 Case 以 ``ReexecutionRunner.run_case()`` 执行，
    由本编排器在调用外以 ``perf_counter`` 计量 ``wall_duration_ms``，Benchmark
    计时不写入 Eval 层。
    """

    def __init__(self, dependencies: EvalDependencies | None = None) -> None:
        """绑定可选真实依赖端口；缺省时完全由隔离 Fixture 驱动。"""
        self._dependencies: EvalDependencies | None = dependencies

    async def run_dataset(
        self,
        dataset_root: Path,
        dataset_name: str,
        *,
        warmup: int,
        repeat: int,
        output_dir: Path,
        baseline_dir: Path | None = None,
    ) -> BenchmarkSnapshot:
        """执行一次完整基线运行并返回快照。

        参数：
            dataset_root: Dataset 根目录。
            dataset_name: Dataset 名称（PR1 固定 ``runtime_core_v1``，CLI 可覆盖）。
            warmup: 预热采样数，必须大于等于 0；预热结果不进入正式统计。
            repeat: 正式采样数，必须大于 0。
            output_dir: 非提交运行工件输出目录（JSONL / JSON / Markdown）。
            baseline_dir: 可选提交基线目录；写入 ``<snapshot-id>.json`` 与
                ``samples/<snapshot-id>.jsonl``。

        返回：
            聚合后的 ``BenchmarkSnapshot``。

        异常：
            ``ValueError``：warmup / repeat 参数非法。
            ``BaselineExperimentError``：Dataset 未知或为空、结果数量不匹配、
                可信结果缺少 Trace、出现不可信结果。
            ``FileExistsError``：目标工件文件已存在，拒绝覆盖。
        """
        if warmup < 0:
            raise ValueError(f"warmup 必须大于等于 0，实际 {warmup}")
        if repeat <= 0:
            raise ValueError(f"repeat 必须大于 0，实际 {repeat}")

        root = Path(dataset_root)
        out = Path(output_dir)
        cases_dir: Path = root / dataset_name / "cases"
        if not cases_dir.is_dir():
            raise BaselineExperimentError(f"未知 Dataset：{dataset_name!r}（{cases_dir} 不存在）")
        cases = load_cases(root, dataset_name)
        if not cases:
            raise BaselineExperimentError(f"Dataset {dataset_name!r} 未包含任何 Case")

        git_commit: str = git_short_commit()
        snapshot_id: str = make_snapshot_id()
        environment: Mapping[str, str] = {
            "python_version": sys.version.split()[0],
            "platform": platform.platform(),
            "config_hash": config_hash(),
            "eval_schema_version": EVAL_SCHEMA_VERSION,
        }

        # 目标工件：output_dir 下 JSONL / 快照 / 报告；baseline_dir 下快照与 JSONL。
        out_samples: Path = out / "samples" / f"{snapshot_id}.jsonl"
        out_snapshot: Path = out / f"{snapshot_id}.json"
        out_report: Path = out / f"{snapshot_id}.md"
        baseline_snapshot: Path | None = (
            Path(baseline_dir) / f"{snapshot_id}.json" if baseline_dir is not None else None
        )
        baseline_samples: Path | None = (
            Path(baseline_dir) / "samples" / f"{snapshot_id}.jsonl" if baseline_dir is not None else None
        )
        _ensure_not_exists((out_samples, out_snapshot, out_report, baseline_snapshot, baseline_samples))

        runner = ReexecutionRunner(dependencies=self._dependencies)
        all_samples: list[BenchmarkSample] = []
        formal_samples: list[BenchmarkSample] = []

        # 每轮按稳定 Case 顺序逐个执行；预热与正式均写 JSONL，正式进入统计。
        for round_index in range(warmup + repeat):
            is_warmup: bool = round_index < warmup
            attempt: int = round_index if is_warmup else round_index - warmup
            for case in cases:
                started: float = time.perf_counter()
                result: EvalResult = await runner.run_case(case)
                wall_duration_ms: float = (time.perf_counter() - started) * 1000.0
                sample = _sample_from_result(
                    result,
                    dataset=dataset_name,
                    case_id=case.case_id,
                    attempt=attempt,
                    is_warmup=is_warmup,
                    wall_duration_ms=wall_duration_ms,
                    git_commit=git_commit,
                    python_version=environment["python_version"],
                    platform_name=environment["platform"],
                    config_hash_value=environment["config_hash"],
                )
                all_samples.append(sample)
                if not is_warmup:
                    formal_samples.append(sample)

        # 可信度校验：正式采样中任何不可信分类或可信结果缺 Trace 都使实验失败。
        for sample in formal_samples:
            if sample.failure_kind not in _TRUSTED_FAILURE_KINDS:
                raise BaselineExperimentError(
                    f"Case {sample.case_id!r} 第 {sample.attempt} 次采样产生不可信结果"
                    f"（{sample.failure_kind}），不生成可用基线"
                )
            if not sample.trace_available:
                raise BaselineExperimentError(
                    f"Case {sample.case_id!r} 第 {sample.attempt} 次采样的可信结果缺少 Trace，"
                    f"Eval 契约被破坏，不生成可用基线"
                )

        # 结果数量校验：每个 Case 必须有恰好 repeat 条正式采样。
        for case in cases:
            count: int = sum(1 for sample in formal_samples if sample.case_id == case.case_id)
            if count != repeat:
                raise BaselineExperimentError(
                    f"Case {case.case_id!r} 正式采样数为 {count}，与 repeat={repeat} 不一致，"
                    f"不生成可用基线"
                )

        samples_path: str = f"samples/{snapshot_id}.jsonl"
        snapshot = build_snapshot(
            snapshot_id=snapshot_id,
            generated_at=datetime.now(timezone.utc).isoformat(),
            git_commit=git_commit,
            dataset=dataset_name,
            environment=environment,
            warmup=warmup,
            repeat=repeat,
            samples=all_samples,
            samples_path=samples_path,
            samples_content_summary={},
        )

        # 写出工件；JSONL 追加写，warmup 与正式均保留为诊断证据。
        write_jsonl(out_samples, all_samples)
        out_snapshot.write_text(
            json.dumps(snapshot.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        out_report.write_text(build_markdown_report(snapshot), encoding="utf-8")

        if baseline_dir is not None:
            assert baseline_snapshot is not None and baseline_samples is not None
            write_jsonl(baseline_samples, all_samples)
            baseline_snapshot.write_text(
                json.dumps(snapshot.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
            )

        # 补充原始证据摘要（行数 / 字节数），重写快照与报告使其自洽。
        updated_snapshot = build_snapshot(
            snapshot_id=snapshot_id,
            generated_at=snapshot.generated_at,
            git_commit=git_commit,
            dataset=dataset_name,
            environment=environment,
            warmup=warmup,
            repeat=repeat,
            samples=all_samples,
            samples_path=samples_path,
            samples_content_summary={
                "line_count": len(all_samples),
                "byte_count": out_samples.stat().st_size,
            },
        )
        out_snapshot.write_text(
            json.dumps(updated_snapshot.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        out_report.write_text(build_markdown_report(updated_snapshot), encoding="utf-8")
        if baseline_dir is not None:
            assert baseline_snapshot is not None
            baseline_snapshot.write_text(
                json.dumps(updated_snapshot.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
            )
        return updated_snapshot


async def run_dataset(
    dataset_root: Path,
    dataset_name: str,
    *,
    warmup: int,
    repeat: int,
    output_dir: Path,
    baseline_dir: Path | None = None,
) -> BenchmarkSnapshot:
    """模块级入口：使用默认隔离 Fixture 编排器执行一次基线运行。"""
    return await EvalBaselineRunner().run_dataset(
        dataset_root,
        dataset_name,
        warmup=warmup,
        repeat=repeat,
        output_dir=output_dir,
        baseline_dir=baseline_dir,
    )


def _print_summary(snapshot: BenchmarkSnapshot) -> None:
    """控制台输出本次基线运行的摘要。"""
    g = snapshot.global_summary
    print("=== dotClaw Eval 基线快照 ===")
    print(f"  Snapshot:  {snapshot.snapshot_id}")
    print(f"  Git:       {snapshot.git_commit}")
    print(f"  Dataset:   {snapshot.dataset}")
    print(f"  Warmup:    {snapshot.warmup} | Repeat: {snapshot.repeat}")
    print(f"  成功率:    {g.passed_count}/{g.sample_count} ({g.success_rate:.2%})")
    print(f"  失败归因:  {dict(g.failure_kinds) or '无'}")
    print(f"  Wall P50:  {g.wall_duration_ms.p50_ms:.1f} ms | P95: {g.wall_duration_ms.p95_ms:.1f} ms")
    print(f"  LLM 调用:  {g.llm_call_count_total} | Tool 调用: {g.tool_call_count_total}")


def main(argv: list[str] | None = None) -> int:
    """CLI 入口：解析参数并执行一次基线运行。"""
    parser = argparse.ArgumentParser(description="dotClaw PR1：当前 Eval 基线快照")
    parser.add_argument("--dataset-root", type=str, default="benchmarks/datasets",
                        help="Dataset 根目录（默认 benchmarks/datasets）")
    parser.add_argument("--dataset", type=str, default="runtime_core_v1",
                        help="Dataset 名称（默认 runtime_core_v1）")
    parser.add_argument("--warmup", type=int, default=5,
                        help="预热采样数，>= 0（默认 5；预热不进入正式统计）")
    parser.add_argument("--repeat", type=int, default=30,
                        help="正式采样数，> 0（默认 30）")
    parser.add_argument("--output", type=str, default=None,
                        help="非提交运行工件输出目录（默认 benchmarks/reports/<snapshot-id>）")
    parser.add_argument("--save-baseline", type=str, default=None,
                        help="可选提交基线目录；写入 <snapshot-id>.json 与 samples/<snapshot-id>.jsonl")
    args = parser.parse_args(argv)

    if args.warmup < 0:
        parser.error("--warmup 必须大于等于 0")
    if args.repeat <= 0:
        parser.error("--repeat 必须大于 0")

    output_dir: Path = Path(args.output) if args.output else Path("benchmarks") / "reports" / make_snapshot_id()
    baseline_dir: Path | None = Path(args.save_baseline) if args.save_baseline else None

    try:
        snapshot: BenchmarkSnapshot = asyncio.run(
            run_dataset(
                Path(args.dataset_root),
                args.dataset,
                warmup=args.warmup,
                repeat=args.repeat,
                output_dir=output_dir,
                baseline_dir=baseline_dir,
            )
        )
    except (BaselineExperimentError, ValueError, FileExistsError) as exc:
        print(f"实验失败：{exc}", file=sys.stderr)
        return 1

    _print_summary(snapshot)
    print(f"  报告:      {output_dir / f'{snapshot.snapshot_id}.md'}")
    if baseline_dir is not None:
        print(f"  基线:      {baseline_dir / f'{snapshot.snapshot_id}.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
