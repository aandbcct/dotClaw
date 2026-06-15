"""benchmarks/cases/tool_dispatch.py — 工具调度延迟评测。

测量 ToolExecutor 从接收调用到开始执行 handler 的纯调度开销。
使用 no-op handler (空操作) 排除工具执行时间。
"""

from __future__ import annotations

import statistics
import time
from pathlib import Path

from benchmarks.stats import p50, p95
from dotclaw.agent.context import AgentContext
from dotclaw.config.settings import JournalConfig
from dotclaw.journal import Journal
from dotclaw.journal.metrics_types import ToolCallMetrics
from dotclaw.journal.snapshot import SnapshotBuilder
from dotclaw.journal.storage import build_run_meta
from dotclaw.tools.executor import ToolExecutor
from dotclaw.tools.handler import BuiltinToolHandler
from dotclaw.tools.registry import ToolRegistry


async def run(
    warmup: int = 3,
    repeat: int = 10,
    project_root: str | Path | None = None,
) -> tuple[ToolCallMetrics, "RunMeta"]:
    """运行工具调度延迟评测，返回 (ToolCallMetrics, RunMeta)。"""
    root = Path(project_root) if project_root else Path(__file__).parent.parent.parent

    registry = ToolRegistry()

    async def _noop(**kwargs) -> str:
        return "ok"

    handler = BuiltinToolHandler(
        name="noop", description="No-op handler for dispatch benchmark",
        parameters={"type": "object", "properties": {}},
        handler_fn=_noop, needs_approval=False, timeout=10.0,
    )
    registry.register(handler)
    executor = ToolExecutor(registry)

    jc = JournalConfig(
        trace_dir="./tmp", snapshot_dir="./tmp",
        console=False, trace=False, snapshot=False,
    )

    durations: list[float] = []
    all_events: list = []

    for i in range(warmup + repeat):
        journal = Journal()
        fake_ctx = AgentContext(
            session_id=f"bench_dispatch_{i}",
            workspace=root, project_root=root,
            model="none", system_prompt="benchmark",
        )
        journal.session_start(fake_ctx, jc)

        t0 = time.perf_counter()
        result = await executor.execute("noop", {}, channel=None, journal=journal)
        dt = (time.perf_counter() - t0) * 1000

        journal.session_end("success", True, dt)

        if i >= warmup:
            durations.append(dt)
            all_events.extend(journal._events)

    meta = build_run_meta(
        run_id=f"bench_tool_dispatch_{time.strftime('%H%M%S')}",
        test_dataset="framework_perf",
        test_dataset_size=repeat,
    )

    builder = SnapshotBuilder(meta, task_count=repeat)
    for event in all_events:
        builder.process(event)

    snapshot = builder.build()
    _print_stats(durations)

    return snapshot.tools, meta


def _print_stats(durations: list[float]) -> None:
    """打印调度延迟统计。"""
    if not durations:
        print("  (no data)")
        return
    s = sorted(durations)
    print(f"  Samples: {len(durations)}")
    print(f"  P50:     {p50(durations):.3f} ms")
    print(f"  P95:     {p95(durations):.3f} ms")
    print(f"  Avg:     {statistics.mean(durations):.3f} ms")
    print(f"  Min:     {min(durations):.3f} ms")
    print(f"  Max:     {max(durations):.3f} ms")


def describe(metrics: ToolCallMetrics) -> str:
    """返回该 case 的 Markdown 详情。"""
    dispatch = metrics.p95_duration_by_tool.get("noop", 0)
    return "\n".join([
        "| Metric | Value |",
        "|--------|-------|",
        f"| Total Calls | {metrics.total_calls} |",
        f"| Success Rate | {metrics.overall_success_rate:.1%} |",
        f"| Dispatch P95 | {dispatch:.3f} ms |",
    ])
