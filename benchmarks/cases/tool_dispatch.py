"""benchmarks/cases/tool_dispatch.py — 工具调度延迟评测。

测量 ToolExecutor 从接收调用到开始执行 handler 的纯调度开销。
使用 no-op handler (空操作) 排除工具执行时间。
"""

from __future__ import annotations

import statistics
import time
from pathlib import Path

from dotclaw.agent.context import AgentContext
from dotclaw.config.settings import JournalConfig
from dotclaw.journal import Journal
from dotclaw.journal.metrics_types import AgentRunSnapshot
from dotclaw.journal.snapshot import SnapshotBuilder
from dotclaw.journal.storage import build_run_meta
from dotclaw.tools.executor import ToolExecutor
from dotclaw.tools.handler import BuiltinToolHandler
from dotclaw.tools.registry import ToolRegistry


async def run(
    warmup: int = 3,
    repeat: int = 10,
    project_root: str | Path | None = None,
) -> AgentRunSnapshot:
    """运行工具调度延迟评测。

    注册一个 no-op handler，调用 ToolExecutor.execute() 并测量
    纯调度开销（handler 执行时间为 0）。

    Args:
        warmup: 前 N 次迭代丢弃（冷启动 warmup）。
        repeat: 实际测量迭代次数。
        project_root: 项目根目录。

    Returns:
        包含工具调度指标的 AgentRunSnapshot。
    """
    root = Path(project_root) if project_root else Path(__file__).parent.parent.parent

    # ── 构建测试环境 ──
    registry = ToolRegistry()

    async def _noop(**kwargs) -> str:
        return "ok"

    handler = BuiltinToolHandler(
        name="noop",
        description="No-op handler for dispatch benchmark",
        parameters={"type": "object", "properties": {}},
        handler_fn=_noop,
        needs_approval=False,
        timeout=10.0,
    )
    registry.register(handler)
    executor = ToolExecutor(registry)

    # Journal config: 不写文件，只收集事件
    jc = JournalConfig(
        trace_dir="./tmp",
        snapshot_dir="./tmp",
        console=False,
        trace=False,
        snapshot=False,
    )

    durations: list[float] = []
    all_events: list = []

    for i in range(warmup + repeat):
        journal = Journal()

        fake_ctx = AgentContext(
            session_id=f"bench_dispatch_{i}",
            workspace=root,
            project_root=root,
            model="none",
            system_prompt="benchmark",
        )

        journal.session_start(fake_ctx, jc)

        t0 = time.perf_counter()
        result = await executor.execute(
            "noop",
            {},
            channel=None,
            journal=journal,
        )
        dt = (time.perf_counter() - t0) * 1000  # ms

        journal.session_end("success", True, dt)

        if i >= warmup:
            durations.append(dt)
            all_events.extend(journal._events)

    # ── 构建 Snapshot（汇总所有有效迭代的事件）──
    meta = build_run_meta(
        run_id=f"bench_tool_dispatch_{time.strftime('%H%M%S')}",
        test_dataset="framework_perf",
        test_dataset_size=repeat,
    )

    builder = SnapshotBuilder(meta, task_count=repeat)
    for event in all_events:
        builder.process(event)

    snapshot = builder.build()

    # 打印统计摘要
    _print_stats(durations)

    return snapshot


def _print_stats(durations: list[float]) -> None:
    """打印调度延迟统计。"""
    if not durations:
        print("  (no data)")
        return

    sorted_d = sorted(durations)
    p50 = sorted_d[int(len(sorted_d) * 0.50)]
    p95 = sorted_d[int(len(sorted_d) * 0.95)]
    avg = statistics.mean(durations)
    stdev = statistics.stdev(durations) if len(durations) > 1 else 0.0
    mn = min(durations)
    mx = max(durations)

    print(f"  Samples: {len(durations)}")
    print(f"  P50:     {p50:.3f} ms")
    print(f"  P95:     {p95:.3f} ms")
    print(f"  Avg:     {avg:.3f} ms")
    print(f"  Min:     {mn:.3f} ms")
    print(f"  Max:     {mx:.3f} ms")
    print(f"  StdDev:  {stdev:.3f} ms")
