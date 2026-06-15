"""benchmarks/runner.py — dotClaw Framework Benchmark 评测总控。

用法:
    python -m benchmarks.runner                          # 运行全部
    python -m benchmarks.runner --filter init_perf       # 指定 case
    python -m benchmarks.runner --warmup 3 --repeat 10   # 调节参数
    python -m benchmarks.runner --baseline baselines/v1.0.json  # 基线对比
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import platform
import sys
import time
from pathlib import Path
from typing import Any

from dotclaw.journal.metrics_types import (
    AgentGeneralMetrics, AgentRunSnapshot,
    InitPerfMetrics, MemoryMetrics, ReactLoopMetrics,
    SkillMetrics, ToolCallMetrics,
)
from dotclaw.journal.storage import (
    build_run_meta, diff_snapshots, load_snapshot, save_snapshot,
)

# ── Case 注册表 ──
_CASES: dict[str, str] = {
    "init_perf": "benchmarks.cases.init_perf",
    "tool_dispatch": "benchmarks.cases.tool_dispatch",
    "llm_stream": "benchmarks.cases.llm_stream",
    "memory_perf": "benchmarks.cases.memory_perf",
    "skill_load": "benchmarks.cases.skill_load",
    "stress": "benchmarks.cases.stress",
}

_CASE_DESCRIPTIONS: dict[str, str] = {
    "init_perf": "Init Performance",
    "tool_dispatch": "Tool Dispatch Latency",
    "llm_stream": "LLM Stream Latency",
    "memory_perf": "Memory Retrieval Performance",
    "skill_load": "Skill Load Performance",
    "stress": "Stress Test",
}


class BenchmarkRunner:
    """评测总控。"""

    def __init__(
        self,
        cases: list[str] | None = None,
        warmup: int = 3,
        repeat: int = 10,
        baseline_path: str | None = None,
        output_dir: str = "benchmarks/reports",
    ):
        self.cases = cases or list(_CASES.keys())
        self.warmup = warmup
        self.repeat = repeat
        self.baseline_path = baseline_path
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.results: dict[str, dict] = {}  # name → {metrics, meta, mod}
        self._project_root = Path(__file__).parent.parent

    async def run(self) -> None:
        meta = build_run_meta(
            run_id=_make_run_id(),
            test_dataset="framework_perf",
            test_dataset_size=len(self.cases),
        )
        self._print_header(meta)

        for i, name in enumerate(self.cases, 1):
            self._print_case_header(i, len(self.cases), name)
            try:
                mod = _import_case(name)
                metrics, case_meta = await mod.run(
                    warmup=self.warmup,
                    repeat=self.repeat,
                    project_root=self._project_root,
                )
                self.results[name] = {"metrics": metrics, "meta": case_meta, "mod": mod}
                self._print_case_result(name, metrics)
            except Exception as exc:
                print(f"\n  [FAILED] {exc}")
                import traceback
                traceback.print_exc()

        report_path = self._generate_report(meta)
        self._save_snapshots()

        if self.baseline_path:
            self._diff_baseline()

        print(f"\n{'='*60}")
        print(f"  Report: {report_path}")
        print(f"  Snapshots: {self.output_dir / 'snapshots'}")
        print(f"{'='*60}")

    # ═══ 输出 ═══

    def _print_header(self, meta: Any) -> None:
        print(f"=== dotClaw Framework Benchmark ===")
        print(f"  Git:      {meta.git_commit}")
        print(f"  Config:   {meta.config_hash}")
        print(f"  Platform: {platform.platform()}")
        print(f"  Python:   {sys.version.split()[0]}")
        print(f"  Cases:    {', '.join(self.cases)}")
        print(f"  Warmup:   {self.warmup} | Repeat: {self.repeat}")
        print()

    @staticmethod
    def _print_case_header(i: int, total: int, name: str) -> None:
        desc = _CASE_DESCRIPTIONS.get(name, name)
        print(f"\n{'─'*60}")
        print(f"  [{i}/{total}] {name} — {desc}")
        print(f"{'─'*60}")

    def _print_case_result(self, name: str, metrics: Any) -> None:
        label, val = _extract_case_result(name, metrics)
        print(f"  [OK] {label}: {val}")

    # ── Snapshot 构建 ──

    def _to_snapshot(self, name: str, info: dict) -> AgentRunSnapshot:
        """将 case 返回的 metrics 转为 AgentRunSnapshot。"""
        metrics = info["metrics"]
        meta = info["meta"]

        if name == "init_perf":
            perf: InitPerfMetrics = metrics
            gen = AgentGeneralMetrics(
                avg_e2e_latency_ms=perf.agent_full_ms,
                p95_e2e_latency_ms=perf.agent_full_p95_ms,
            )
        elif name == "tool_dispatch":
            gen = AgentGeneralMetrics()
        elif name == "llm_stream":
            gen: AgentGeneralMetrics = metrics
        elif name == "memory_perf":
            by_size: dict[str, MemoryMetrics] = metrics
            large = by_size.get("large", MemoryMetrics())
            gen = AgentGeneralMetrics(
                avg_e2e_latency_ms=large.avg_retrieval_ms,
                p95_e2e_latency_ms=large.p95_retrieval_ms,
            )
            return AgentRunSnapshot(
                run_id=meta.run_id, timestamp=meta.timestamp,
                git_commit=meta.git_commit, config_hash=meta.config_hash,
                test_dataset=meta.test_dataset, test_dataset_size=meta.test_dataset_size,
                react=ReactLoopMetrics(), tools=ToolCallMetrics(),
                skills=SkillMetrics(), memory=large, general=gen,
            )
        elif name == "skill_load":
            by_count: dict[int, SkillMetrics] = metrics
            sk = by_count.get(100, SkillMetrics())
            gen = AgentGeneralMetrics(
                avg_e2e_latency_ms=sk.avg_body_load_ms,
                p95_e2e_latency_ms=0.0,
            )
            return AgentRunSnapshot(
                run_id=meta.run_id, timestamp=meta.timestamp,
                git_commit=meta.git_commit, config_hash=meta.config_hash,
                test_dataset=meta.test_dataset, test_dataset_size=meta.test_dataset_size,
                react=ReactLoopMetrics(), tools=ToolCallMetrics(),
                skills=sk, memory=MemoryMetrics(), general=gen,
            )
        elif name == "stress":
            gen = AgentGeneralMetrics()
        else:
            gen = AgentGeneralMetrics()

        tools = metrics if name == "tool_dispatch" else ToolCallMetrics()
        return AgentRunSnapshot(
            run_id=meta.run_id, timestamp=meta.timestamp,
            git_commit=meta.git_commit, config_hash=meta.config_hash,
            test_dataset=meta.test_dataset, test_dataset_size=meta.test_dataset_size,
            react=ReactLoopMetrics(), tools=tools,
            skills=SkillMetrics(), memory=MemoryMetrics(), general=gen,
        )

    # ── 报告 ──

    def _generate_report(self, meta: Any) -> Path:
        from datetime import datetime

        lines: list[str] = []
        lines.append("# dotClaw Framework Benchmark Report")
        lines.append("")
        lines.append(f"> **Generated**: {datetime.now().isoformat()}")
        lines.append(f"> **Git**: `{meta.git_commit}`")
        lines.append(f"> **Config**: `{meta.config_hash}`")
        lines.append(f"> **Platform**: {platform.platform()}")
        lines.append(f"> **Python**: {sys.version.split()[0]}")
        lines.append(f"> **Warmup**: {self.warmup} | **Repeat**: {self.repeat}")
        lines.append("")

        lines.append("## Summary")
        lines.append("")
        lines.append("| # | Case | Description | Key Metric | Value |")
        lines.append("|---|------|-------------|------------|-------|")
        for i, name in enumerate(self.cases, 1):
            if name not in self.results:
                lines.append(f"| {i} | `{name}` | {_CASE_DESCRIPTIONS.get(name, '')} | — | FAILED |")
                continue
            info = self.results[name]
            desc = _CASE_DESCRIPTIONS.get(name, name)
            label, val = _extract_case_result(name, info["metrics"])
            lines.append(f"| {i} | `{name}` | {desc} | {label} | {val} |")
        lines.append("")

        for name in self.cases:
            if name not in self.results:
                continue
            info = self.results[name]
            desc = _CASE_DESCRIPTIONS.get(name, name)
            lines.append(f"## {name} — {desc}")
            lines.append("")
            if hasattr(info["mod"], "describe"):
                lines.append(info["mod"].describe(info["metrics"]))
            lines.append("")

        lines.append("---")
        lines.append("")
        lines.append("[EXT] = External dependency (network/API latency included)")
        lines.append("")

        ts = time.strftime("%Y%m%d_%H%M%S")
        report_path = self.output_dir / f"benchmark_report_{ts}.md"
        report_path.write_text("\n".join(lines), encoding="utf-8")
        return report_path

    # ── Snapshot 持久化 ──

    def _save_snapshots(self) -> None:
        snap_dir = self.output_dir / "snapshots"
        snap_dir.mkdir(exist_ok=True)
        for name, info in self.results.items():
            snapshot = self._to_snapshot(name, info)
            path = save_snapshot(snapshot, str(snap_dir))
            print(f"  Snapshot: {path}")

    # ── 基线对比 ──

    def _diff_baseline(self) -> None:
        print(f"\n{'='*60}")
        print(f"  Diff vs Baseline: {self.baseline_path}")
        print(f"{'='*60}")

        baseline = load_snapshot(self.baseline_path)
        for name, info in self.results.items():
            snapshot = self._to_snapshot(name, info)
            diff_lines = diff_snapshots(baseline, snapshot, threshold=5.0)
            if diff_lines:
                print(f"\n  [{name}]:")
                for line in diff_lines:
                    print(f"    {line}")
            else:
                print(f"\n  [{name}]: no significant change")


# ═══════════════════════════════════════════════════════════════════
# 帮助函数
# ═══════════════════════════════════════════════════════════════════


def _import_case(name: str):
    return importlib.import_module(_CASES[name])


def _extract_case_result(name: str, metrics: Any) -> tuple[str, str]:
    """从 metrics 对象提取 Summary 表的指标。"""
    if name == "init_perf":
        from dotclaw.journal.metrics_types import InitPerfMetrics
        m: InitPerfMetrics = metrics
        return "Agent Init P95", f"{m.agent_full_p95_ms:.1f} ms"
    elif name == "tool_dispatch":
        from dotclaw.journal.metrics_types import ToolCallMetrics
        m: ToolCallMetrics = metrics
        d = m.p95_duration_by_tool.get("noop", 0)
        return "Dispatch P95", f"{d:.1f} ms"
    elif name == "llm_stream":
        from dotclaw.journal.metrics_types import AgentGeneralMetrics
        m: AgentGeneralMetrics = metrics
        return "TTFT [EXT]", f"{m.avg_ttft_ms:.1f} ms"
    elif name == "memory_perf":
        from dotclaw.journal.metrics_types import MemoryMetrics
        m: MemoryMetrics = metrics.get("large", MemoryMetrics()) if isinstance(metrics, dict) else metrics
        return "P95 Retrieval (10K)", f"{m.p95_retrieval_ms:.1f} ms"
    elif name == "skill_load":
        from dotclaw.journal.metrics_types import SkillMetrics
        m: SkillMetrics = metrics.get(100, SkillMetrics()) if isinstance(metrics, dict) else metrics
        return "Scan 100 Skills", f"{m.avg_body_load_ms:.1f} ms (P50)"
    elif name == "stress":
        return "E2E P50", "—"
    return "—", "—"


def _make_run_id() -> str:
    return f"bench_{time.strftime('%Y%m%d_%H%M%S')}"


def main():
    parser = argparse.ArgumentParser(description="dotClaw Framework Benchmark")
    parser.add_argument("--filter", type=str, default=None)
    parser.add_argument("--warmup", type=int, default=3)
    parser.add_argument("--repeat", type=int, default=10)
    parser.add_argument("--baseline", type=str, default=None)
    parser.add_argument("--output", type=str, default="benchmarks/reports")
    args = parser.parse_args()

    case_names = args.filter.split(",") if args.filter else None
    if case_names:
        for name in case_names:
            if name not in _CASES:
                print(f"Error: unknown case '{name}'. Available: {', '.join(_CASES.keys())}")
                sys.exit(1)

    runner = BenchmarkRunner(
        cases=case_names,
        warmup=args.warmup,
        repeat=args.repeat,
        baseline_path=args.baseline,
        output_dir=args.output,
    )
    asyncio.run(runner.run())


if __name__ == "__main__":
    main()
