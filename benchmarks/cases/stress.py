"""benchmarks/cases/stress.py — 压力测试。

测量 dotClaw 框架在极端条件下的稳定性：
1. 并发工具调用（50 个 no-op）
2. 大上下文构建
3. 超长 ReAct 循环（20 步）
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from benchmarks.stats import p50, p95
from dotclaw.journal.metrics_types import AgentRunSnapshot
from dotclaw.journal.storage import build_run_meta


async def run(
    warmup: int = 3,
    repeat: int = 10,
    project_root: str | Path | None = None,
    output_dir: str | None = None,
) -> tuple[AgentRunSnapshot, "RunMeta"]:
    """运行压力测试。

    三个场景：并发工具、大上下文、超长 ReAct。
    """
    root = Path(project_root) if project_root else Path(__file__).parent.parent.parent

    results: dict[str, list[float]] = {}

    # ── 场景 1: 并发工具调用 ──
    print("\n  --- Scenario 1: Concurrent Tool Calls (50 no-ops) ---")
    concurrent_durs = []
    for i in range(warmup + repeat):
        dt = await _bench_concurrent_tools()
        if i >= warmup:
            concurrent_durs.append(dt)
    results["concurrent"] = concurrent_durs
    _print_scenario("Concurrent 50", concurrent_durs)

    # ── 场景 2: 大上下文构建 ──
    print("\n  --- Scenario 2: Large Context (50KB/100KB) ---")
    ctx_durs: dict[str, list[float]] = {}
    for size_kb in [50, 100]:
        durs = []
        for i in range(warmup + repeat):
            dt = _bench_large_context(root, size_kb)
            if i >= warmup:
                durs.append(dt)
        ctx_durs[f"ctx_{size_kb}kb"] = durs
        _print_scenario(f"Context {size_kb}KB", durs)

    # ── 场景 3: 超长 ReAct 循环 ──
    print("\n  --- Scenario 3: Long ReAct (20 steps) ---")
    react_durs = []
    for i in range(warmup + repeat):
        dt = await _bench_long_react(root)
        if i >= warmup:
            react_durs.append(dt)
    results["long_react"] = react_durs
    _print_scenario("ReAct 20 steps", react_durs)

    # ── 构建 Snapshot ──
    meta = build_run_meta(
        run_id=f"bench_stress_{time.strftime('%H%M%S')}",
        test_dataset="framework_perf",
        test_dataset_size=repeat,
    )

    from dotclaw.journal.metrics_types import (
        AgentGeneralMetrics, ReactLoopMetrics,
        ToolCallMetrics, SkillMetrics, MemoryMetrics,
    )

    snapshot = AgentRunSnapshot(
        meta=meta,
        react=ReactLoopMetrics(
            total_loops=20 * repeat,
            task_completion_rate=1.0,
        ),
        tools=ToolCallMetrics(
            total_calls=50 * repeat,
            overall_success_rate=1.0,
            avg_duration_by_tool={
                "concurrent_50": p50(results.get("concurrent", [])),
                **{k: p50(v) for k, v in ctx_durs.items() if v},
            },
            p95_duration_by_tool={
                "concurrent_50": p95(results.get("concurrent", [])),
                **{k: p95(v) for k, v in ctx_durs.items() if v},
            },
        ),
        skills=SkillMetrics(),
        memory=MemoryMetrics(),
        general=AgentGeneralMetrics(
            avg_e2e_latency_ms=p50(results.get("long_react", [])),
            p95_e2e_latency_ms=p95(results.get("long_react", [])),
        ),
    )
    if output_dir:
        from dotclaw.journal.storage import save_snapshot
        save_snapshot(snapshot, str(output_dir))
    return snapshot, meta


# ═══════════════════════════════════════════════════════════════════
# 场景实现
# ═══════════════════════════════════════════════════════════════════


async def _bench_concurrent_tools() -> float:
    """测量 50 个 no-op 工具并发执行的耗时。"""
    from dotclaw.tools.registry import ToolRegistry
    from dotclaw.tools.executor import ToolExecutor
    from dotclaw.tools.handler import BuiltinToolHandler
    from dotclaw.config.settings import JournalConfig
    from dotclaw.journal import Journal
    from pathlib import Path

    registry = ToolRegistry()
    for j in range(50):
        def _make_noop(idx: int):
            async def _noop(**kwargs) -> str:
                return f"ok_{idx}"
            return _noop
        handler = BuiltinToolHandler(
            name=f"stress_noop_{j}",
            description="Stress test no-op",
            parameters={"type": "object", "properties": {}},
            handler_fn=_make_noop(j),
            needs_approval=False,
            timeout=10.0,
        )
        registry.register(handler)

    executor = ToolExecutor(registry)
    jc = JournalConfig(trace=False, snapshot=False, console=False, trace_dir="./tmp", snapshot_dir="./tmp")

    journal = Journal()
    journal.session_start(
        session_id="bench_stress_concurrent",
        request_id="req-001",
        model="none",
        config=jc,
    )

    t0 = time.perf_counter()
    tasks = [
        executor.execute(f"stress_noop_{j}", {}, channel=None, journal=journal)
        for j in range(50)
    ]
    await asyncio.gather(*tasks)
    dt = (time.perf_counter() - t0) * 1000

    journal.session_end("success", True, dt)
    return dt


def _bench_large_context(root: Path, size_kb: int) -> float:
    """测量大上下文构建耗时。"""
    from dotclaw.agent.slotContext import ContextAssembler, SlotContext

    large_prompt = "You are a helpful assistant. " * (size_kb * 50)

    # 构建大 IdentitySlot（直接设置缓存）
    from dotclaw.agent.slotContextImp import IdentitySlot
    slot = IdentitySlot()
    slot._cached = large_prompt
    slot._cache_valid = True

    assembler = ContextAssembler([slot])
    ctx = SlotContext(
        query="bench",
        request_id="r1",
        session_id="bench_stress_ctx",
        project_root=root,
        max_context_tokens=100000,
    )

    t0 = time.perf_counter()
    import asyncio
    _ = asyncio.get_event_loop().run_until_complete(assembler.build_system_prompt(ctx))
    return (time.perf_counter() - t0) * 1000


async def _bench_long_react(root: Path) -> float:
    """测量 20 步 ReAct 循环的框架开销（无 LLM 调用）。"""
    from dotclaw.tools.registry import ToolRegistry
    from dotclaw.tools.handler import BuiltinToolHandler
    from dotclaw.config.settings import JournalConfig
    from dotclaw.journal import Journal

    # 注册一个 no-op
    registry = ToolRegistry()
    async def _noop(**kwargs) -> str:
        return "ok"
    handler = BuiltinToolHandler(
        name="noop", description="no-op",
        parameters={"type": "object", "properties": {}},
        handler_fn=_noop,
        needs_approval=False, timeout=10.0,
    )
    registry.register(handler)

    jc = JournalConfig(trace=False, snapshot=False, console=False, trace_dir="./tmp", snapshot_dir="./tmp")
    journal = Journal()
    journal.session_start(
        session_id="bench_stress_react",
        request_id="req-001",
        model="none",
        config=jc,
    )

    from dotclaw.tools.executor import ToolExecutor
    executor = ToolExecutor(registry)

    t0 = time.perf_counter()
    for step in range(20):
        journal.loop_start()
        journal.tool_start("noop", args={})
        await executor.execute("noop", {}, channel=None, journal=journal)
        journal.loop_end(action="tool_call")
    dt = (time.perf_counter() - t0) * 1000

    journal.session_end("success", True, dt)
    return dt


# ═══════════════════════════════════════════════════════════════════
# 辅助
# ═══════════════════════════════════════════════════════════════════


def _print_scenario(label: str, durs: list[float]) -> None:
    if not durs:
        print(f"  {label}: no data")
        return
    print(f"  {label}: P50={p50(durs):.1f}ms  P95={p95(durs):.1f}ms  "
          f"Avg={sum(durs)/len(durs):.1f}ms  Min={min(durs):.1f}ms  Max={max(durs):.1f}ms")
