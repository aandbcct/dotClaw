"""benchmarks/cases/init_perf.py — 初始化性能评测。

测量 dotClaw 各核心组件从构造到就绪的耗时。
"""

from __future__ import annotations

import statistics
import time
from pathlib import Path

from dotclaw.journal.metrics_types import AgentRunSnapshot
from dotclaw.journal.storage import build_run_meta


async def run(
    warmup: int = 3,
    repeat: int = 10,
    project_root: str | Path | None = None,
) -> AgentRunSnapshot:
    """运行初始化性能评测。

    测量各核心组件构造耗时。

    Args:
        warmup: 前 N 次迭代丢弃。
        repeat: 实际测量迭代次数。
        project_root: 项目根目录。

    Returns:
        AgentRunSnapshot。
    """
    root = Path(project_root) if project_root else Path(__file__).parent.parent.parent

    results: dict[str, list[float]] = {}
    component_labels = [
        "config_load", "llm_build", "skill_scan",
        "tool_build", "session_mgr", "prompt_builder",
        "memory_build", "agent_full",
    ]
    for name in component_labels:
        results[name] = []

    for i in range(warmup + repeat):
        times = await _measure_one_iteration(root)
        if i >= warmup:
            for name, dt in times.items():
                results[name].append(dt)

    _print_stats(results)

    # ── 构建 Snapshot ──
    meta = build_run_meta(
        run_id=f"bench_init_perf_{time.strftime('%H%M%S')}",
        test_dataset="framework_perf",
        test_dataset_size=repeat,
    )

    from dotclaw.journal.metrics_types import (
        AgentGeneralMetrics, ReactLoopMetrics,
        ToolCallMetrics, SkillMetrics, MemoryMetrics,
        InitPerfMetrics,
    )

    # 构建 InitPerfMetrics（组件分项明细，不进 snapshot）
    perf = InitPerfMetrics(
        config_load_ms=_p50(results.get("config_load", [])),
        llm_build_ms=_p50(results.get("llm_build", [])),
        skill_scan_ms=_p50(results.get("skill_scan", [])),
        tool_build_ms=_p50(results.get("tool_build", [])),
        session_mgr_ms=_p50(results.get("session_mgr", [])),
        prompt_builder_ms=_p50(results.get("prompt_builder", [])),
        memory_build_ms=_p50(results.get("memory_build", [])),
        agent_full_ms=_p50(results.get("agent_full", [])),
    )

    snapshot = AgentRunSnapshot(
        run_id=meta.run_id,
        timestamp=meta.timestamp,
        git_commit=meta.git_commit,
        config_hash=meta.config_hash,
        test_dataset=meta.test_dataset,
        test_dataset_size=meta.test_dataset_size,
        react=ReactLoopMetrics(),
        tools=ToolCallMetrics(),
        skills=SkillMetrics(),
        memory=MemoryMetrics(),
        general=AgentGeneralMetrics(
            avg_e2e_latency_ms=perf.agent_full_ms,
            p95_e2e_latency_ms=_p95(results.get("agent_full", [])),
        ),
    )
    return snapshot


# ═══════════════════════════════════════════════════════════════════
# 测量
# ═══════════════════════════════════════════════════════════════════


async def _measure_one_iteration(root: Path) -> dict[str, float]:
    """测量一轮所有组件的初始化耗时。"""
    times: dict[str, float] = {}

    # ── 1. Config 加载 ──
    t0 = time.perf_counter()
    from dotclaw.config.settings import load_config
    config = load_config(str(root / "config.yaml"))
    times["config_load"] = (time.perf_counter() - t0) * 1000

    # ── 2. LLMProxy ──
    from dotclaw.agent.factory import _build_llm
    t0 = time.perf_counter()
    llm = _build_llm(config, root)
    times["llm_build"] = (time.perf_counter() - t0) * 1000

    # ── 3. SkillRegistry（扫描 benchmark dataset）──
    t0 = time.perf_counter()
    skill_registry = _scan_bench_skills(config, root)
    times["skill_scan"] = (time.perf_counter() - t0) * 1000

    # ── 4. ToolExecutor ──
    t0 = time.perf_counter()
    tool_executor = _build_tools(config, skill_registry)
    times["tool_build"] = (time.perf_counter() - t0) * 1000

    # ── 5. SessionManager ──
    from dotclaw.memory.store import SessionManager
    t0 = time.perf_counter()
    session_mgr = SessionManager(config.session.directory)
    times["session_mgr"] = (time.perf_counter() - t0) * 1000

    # ── 6. PromptBuilder ──
    from dotclaw.agent.factory import _build_prompt_builder
    t0 = time.perf_counter()
    prompt_builder = _build_prompt_builder()
    times["prompt_builder"] = (time.perf_counter() - t0) * 1000

    # ── 7. MemoryManager（异步）──
    from dotclaw.agent.factory import _build_memory
    t0 = time.perf_counter()
    await _build_memory(config, llm, root)
    times["memory_build"] = (time.perf_counter() - t0) * 1000

    # ── 8. 完整 Agent 装配 ──
    t0 = time.perf_counter()
    await _assemble_agent(root, config, llm, session_mgr, skill_registry)
    times["agent_full"] = (time.perf_counter() - t0) * 1000

    return times


def _scan_bench_skills(config, root: Path):
    """使用 benchmark dataset 扫描 skills。"""
    if not config.skills.enabled:
        return None
    from dotclaw.skills.scanner import SkillScanner
    from dotclaw.skills.registry import SkillRegistry
    bench_dir = str(root / "benchmarks" / "dataset" / "sample_skills")
    scanner = SkillScanner([bench_dir], skip_prefix=config.skills.skip_prefix)
    metas = scanner.scan()
    registry = SkillRegistry()
    for meta in metas:
        registry.register(meta)
    return registry


def _build_tools(config, skill_registry):
    """构建 ToolExecutor。"""
    from dotclaw.tools.registry import ToolRegistry
    from dotclaw.tools.executor import ToolExecutor
    from dotclaw.tools.approval import ApprovalManager
    from dotclaw.tools.builtin import register_all
    from dotclaw.tools.parser import SkillParser

    registry = ToolRegistry()
    if config.tools.builtin_enabled:
        register_all(registry)
    for tool_name in config.tools.disabled_tools:
        registry.unregister(tool_name)
    approval_mgr = ApprovalManager(approval_commands=config.tools.approval_commands)
    skill_parser = SkillParser(skill_registry) if skill_registry else None
    return ToolExecutor(registry=registry, approval_manager=approval_mgr, skill_parser=skill_parser)


async def _assemble_agent(root, config, llm, session_mgr, skill_registry):
    """测量 Agent 完整装配耗时。"""
    from dotclaw.agent import Agent, load_agent_config
    from dotclaw.agent.factory import _build_memory

    agent_config = load_agent_config(agent_id="default")
    tool_executor = _build_tools(config, skill_registry)
    mem_mgr, mem_dream = await _build_memory(config, llm, root)

    agent = Agent(
        agent_config=agent_config, config=config, llm=llm,
        session_mgr=session_mgr, channel=None,
        tool_executor=tool_executor, prompt_builder=None,
        memory_mgr=mem_mgr, skill_registry=skill_registry,
        mcp_provider=None, memory_dream=mem_dream,
        mcp_task=None,
    )
    return agent


# ═══════════════════════════════════════════════════════════════════
# 统计与输出
# ═══════════════════════════════════════════════════════════════════


def _print_stats(results: dict[str, list[float]]) -> None:
    """打印组件初始化耗时统计表。"""
    order = [
        ("config_load", "Config Load"),
        ("llm_build", "LLMProxy"),
        ("skill_scan", "SkillRegistry (100)"),
        ("tool_build", "ToolExecutor"),
        ("session_mgr", "SessionManager"),
        ("prompt_builder", "PromptBuilder"),
        ("memory_build", "MemoryManager"),
        ("agent_full", "Agent (complete)"),
    ]

    w = 22
    print(f"  {'Component':<{w}} {'P50':>8} {'P95':>8} {'Min':>8} {'Max':>8}")
    print(f"  {'-'*w} {'-'*8} {'-'*8} {'-'*8} {'-'*8}")
    total_p50 = 0.0
    for key, label in order:
        durs = results.get(key, [])
        if not durs:
            continue
        p50 = _p50(durs); p95 = _p95(durs); mn = min(durs); mx = max(durs)
        print(f"  {label:<{w}} {p50:>7.1f} {p95:>7.1f} {mn:>7.1f} {mx:>7.1f}")
        if key != "agent_full":
            total_p50 += p50
    print(f"  {'-'*w} {'-'*8} {'-'*8} {'-'*8} {'-'*8}")
    agent_p50 = _p50(results.get("agent_full", []))
    print(f"  {'TOTAL (sum)':<{w}} {total_p50:>7.1f}")
    print(f"  {'Agent (measured)':<{w}} {agent_p50:>7.1f}")


def _p50(values: list[float]) -> float:
    if not values: return 0.0
    s = sorted(values)
    return s[int(len(s) * 0.50)]


def _p95(values: list[float]) -> float:
    if not values: return 0.0
    s = sorted(values)
    return s[int(len(s) * 0.95)]
