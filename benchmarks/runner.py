"""benchmarks/runner.py — dotClaw Framework Benchmark 评测总控。

用法:
    python -m benchmarks.runner                          # 运行全部
    python -m benchmarks.runner --filter init_perf       # 指定 case
    python -m benchmarks.runner --warmup 3 --repeat 10   # 调节参数
    python -m benchmarks.runner --baseline baselines/v1.0.json  # 基线对比
    python -m benchmarks.runner --output reports/2026-06-15     # 输出目录
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

from dotclaw.journal.metrics_types import AgentRunSnapshot
from dotclaw.journal.storage import (
    build_run_meta,
    diff_snapshots,
    load_snapshot,
    save_snapshot,
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

# Case 描述（用于报告）
_CASE_DESCRIPTIONS: dict[str, str] = {
    "init_perf": "Init Performance",
    "tool_dispatch": "Tool Dispatch Latency",
    "llm_stream": "LLM Stream Latency",
    "memory_perf": "Memory Retrieval Performance",
    "skill_load": "Skill Load Performance",
    "stress": "Stress Test",
}

# 每个 case 的核心指标提取
_KEY_METRIC_LABELS: dict[str, str] = {
    "init_perf": "Agent Init",
    "tool_dispatch": "Dispatch Overhead",
    "llm_stream": "TTFT [EXT]",
    "memory_perf": "P95 Retrieval (10K)",
    "skill_load": "Scan 100 Skills",
    "stress": "E2E (concurrent)",
}


class BenchmarkRunner:
    """评测总控：运行 case → 收集 snapshot → 生成报告 → 基线对比。"""

    def __init__(
        self,
        cases: list[str] | None = None,
        warmup: int = 1,
        repeat: int = 2,
        baseline_path: str | None = None,
        output_dir: str = "benchmarks/reports",
    ):
        self.cases = cases or list(_CASES.keys())
        self.warmup = warmup
        self.repeat = repeat
        self.baseline_path = baseline_path
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.results: dict[str, AgentRunSnapshot] = {}
        self._project_root = Path(__file__).parent.parent

    async def run(self) -> None:
        """主流程：依次执行 case，生成报告，可选基线对比。"""
        meta = build_run_meta(
            run_id=_make_run_id(),
            test_dataset="framework_perf",
            test_dataset_size=len(self.cases),
        )

        # ── 打印头信息 ──
        self._print_header(meta)

        # ── 运行 case ──
        for i, name in enumerate(self.cases, 1):
            self._print_case_header(i, len(self.cases), name)
            try:
                snapshot = await self._run_case(name)
                self.results[name] = snapshot
                self._print_case_result(name, snapshot)
            except Exception as exc:
                print(f"\n  [FAILED] {exc}")
                import traceback
                traceback.print_exc()

        # ── 生成报告 ──
        report_path = self._generate_report(meta)

        # ── 保存 snapshot ──
        self._save_snapshots()

        # ── 基线对比 ──
        if self.baseline_path:
            self._diff_baseline()

        # ── 结尾 ──
        print(f"\n{'='*60}")
        print(f"  Report: {report_path}")
        print(f"  Snapshots: {self.output_dir / 'snapshots'}")
        print(f"{'='*60}")

    # ═══ 内部 ═══

    async def _run_case(self, name: str) -> AgentRunSnapshot:
        """动态导入 case 模块并调用其 run() 函数。"""
        module_path = _CASES[name]
        mod = importlib.import_module(module_path)

        if not hasattr(mod, "run"):
            raise AttributeError(f"{module_path} missing run() function")

        return await mod.run(
            warmup=self.warmup,
            repeat=self.repeat,
            project_root=self._project_root,
        )

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

    @staticmethod
    def _print_case_result(name: str, s: AgentRunSnapshot) -> None:
        label = _KEY_METRIC_LABELS.get(name, "Key Metric")

        # 提取核心指标
        if name == "init_perf":
            val = f"{s.general.p95_e2e_latency_ms:.1f}ms (Agent Init P95)"
        elif name == "tool_dispatch":
            dispatch = s.tools.p95_duration_by_tool.get("noop", 0)
            val = f"{dispatch:.1f}ms (Dispatch P95)"
        elif name == "llm_stream":
            val = f"TTFT={s.general.avg_ttft_ms:.1f}ms, TPS={s.general.avg_tps:.1f}"
        elif name == "memory_perf":
            val = f"{s.memory.p95_retrieval_ms:.1f}ms (P95)"
        elif name == "skill_load":
            val = f"{s.skills.avg_body_load_ms:.1f}ms (P50)"
        elif name == "stress":
            val = f"{s.general.avg_e2e_latency_ms:.1f}ms (E2E P50)"
        else:
            val = "—"
        print(f"  [OK] {label}: {val}")

    # ── 报告生成 ──

    def _generate_report(self, meta: Any) -> Path:
        """生成 Markdown 报告。"""
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

        # ── 汇总表 ──
        lines.append("## Summary")
        lines.append("")
        lines.append("| # | Case | Description | Key Metric | Value |")
        lines.append("|---|------|-------------|------------|-------|")
        for i, name in enumerate(self.cases, 1):
            if name not in self.results:
                lines.append(f"| {i} | `{name}` | {_CASE_DESCRIPTIONS.get(name, '')} | — | FAILED |")
                continue
            s = self.results[name]
            desc = _CASE_DESCRIPTIONS.get(name, name)
            metric, val = self._extract_summary(name, s)
            lines.append(f"| {i} | `{name}` | {desc} | {metric} | {val} |")
        lines.append("")

        # ── 各 case 详情 ──
        for name in self.cases:
            if name not in self.results:
                continue
            lines.extend(self._case_detail(name, self.results[name]))

        # ── 外部依赖标记 ──
        lines.append("---")
        lines.append("")
        lines.append("[EXT] = External dependency (network/API latency included)")
        lines.append("")

        # ── 写文件 ──
        ts = time.strftime("%Y%m%d_%H%M%S")
        report_path = self.output_dir / f"benchmark_report_{ts}.md"
        report_path.write_text("\n".join(lines), encoding="utf-8")
        return report_path

    def _extract_summary(self, name: str, s: AgentRunSnapshot) -> tuple[str, str]:
        """提取每个 case 的核心指标用于汇总表。"""
        if name == "init_perf":
            return "Agent Init P95", f"{s.general.p95_e2e_latency_ms:.1f} ms"
        elif name == "tool_dispatch":
            d = s.tools.p95_duration_by_tool.get("noop", 0)
            return "Dispatch P95", f"{d:.1f} ms"
        elif name == "llm_stream":
            return "TTFT [EXT]", f"{s.general.avg_ttft_ms:.1f} ms"
        elif name == "memory_perf":
            return "P95 Retrieval", f"{s.memory.p95_retrieval_ms:.1f} ms"
        elif name == "skill_load":
            return "Scan P50", f"{s.skills.avg_body_load_ms:.1f} ms"
        elif name == "stress":
            return "E2E P50", f"{s.general.avg_e2e_latency_ms:.1f} ms"
        return "—", "—"

    def _case_detail(self, name: str, s: AgentRunSnapshot) -> list[str]:
        """生成某个 case 的详细 Markdown section。"""
        lines: list[str] = []
        desc = _CASE_DESCRIPTIONS.get(name, name)
        lines.append(f"## {name} — {desc}")
        lines.append("")
        lines.append(f"| Metric | Value |")
        lines.append(f"|--------|-------|")

        # ReAct 指标
        lines.append(f"| ReAct — Total Loops | {s.react.total_loops} |")
        lines.append(f"| ReAct — Completion Rate | {s.react.task_completion_rate:.1%} |")

        # LLM 指标
        total_tokens = s.general.total_input_tokens + s.general.total_output_tokens
        lines.append(f"| LLM — Avg TTFT | {s.general.avg_ttft_ms:.1f} ms |")
        lines.append(f"| LLM — Avg TPS | {s.general.avg_tps:.1f} |")
        lines.append(f"| LLM — Total Tokens | {total_tokens} |")

        # 工具指标
        if s.tools.total_calls > 0:
            lines.append(f"| Tools — Total Calls | {s.tools.total_calls} |")
            lines.append(f"| Tools — Success Rate | {s.tools.overall_success_rate:.1%} |")

        # 记忆指标
        if s.memory.total_retrievals > 0:
            lines.append(f"| Memory — Retrievals | {s.memory.total_retrievals} |")
            lines.append(f"| Memory — Hit Rate | {s.memory.hit_rate:.1%} |")
            lines.append(f"| Memory — Avg Duration | {s.memory.avg_retrieval_ms:.1f} ms |")
            lines.append(f"| Memory — P95 Duration | {s.memory.p95_retrieval_ms:.1f} ms |")

        # Skill 指标
        if s.skills.total_triggers > 0:
            lines.append(f"| Skills — Triggers | {s.skills.total_triggers} |")
            lines.append(f"| Skills — Cache Hit Rate | {s.skills.body_cache_hit_rate:.1%} |")

        # E2E
        lines.append(f"| General — Avg E2E | {s.general.avg_e2e_latency_ms:.1f} ms |")
        lines.append(f"| General — P95 E2E | {s.general.p95_e2e_latency_ms:.1f} ms |")
        lines.append("")

        return lines

    # ── Snapshot 持久化 ──

    def _save_snapshots(self) -> None:
        """将所有 case 的 snapshot 保存到输出目录。"""
        snap_dir = self.output_dir / "snapshots"
        snap_dir.mkdir(exist_ok=True)
        for name, snapshot in self.results.items():
            path = save_snapshot(snapshot, str(snap_dir))
            print(f"  Snapshot: {path}")

    # ── 基线对比 ──

    def _diff_baseline(self) -> None:
        """对比当前结果与基线 snapshot。"""
        print(f"\n{'='*60}")
        print(f"  Diff vs Baseline: {self.baseline_path}")
        print(f"{'='*60}")

        baseline = load_snapshot(self.baseline_path)

        for name, snapshot in self.results.items():
            diff_lines = diff_snapshots(baseline, snapshot, threshold=5.0)
            if diff_lines:
                print(f"\n  [{name}]:")
                for line in diff_lines:
                    print(f"    {line}")
            else:
                print(f"\n  [{name}]: no significant change")


# ═══════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════


def _make_run_id() -> str:
    return f"bench_{time.strftime('%Y%m%d_%H%M%S')}"


def main():
    parser = argparse.ArgumentParser(
        description="dotClaw Framework Benchmark",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"Available cases: {', '.join(_CASES.keys())}",
    )
    parser.add_argument(
        "--filter", type=str, default=None,
        help="Comma-separated case names to run (default: all)",
    )
    parser.add_argument(
        "--warmup", type=int, default=3,
        help="Warmup iterations to discard (default: 3)",
    )
    parser.add_argument(
        "--repeat", type=int, default=10,
        help="Measurement iterations (default: 10)",
    )
    parser.add_argument(
        "--baseline", type=str, default=None,
        help="Path to baseline snapshot JSON for comparison",
    )
    parser.add_argument(
        "--output", type=str, default="benchmarks/reports",
        help="Output directory for reports and snapshots (default: benchmarks/reports)",
    )
    args = parser.parse_args()

    case_names = args.filter.split(",") if args.filter else None

    # 验证 case 名称
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
