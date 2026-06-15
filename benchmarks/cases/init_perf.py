"""benchmarks/cases/init_perf.py — 初始化性能评测。

测量 dotClaw 各核心组件从构造到就绪的耗时。
"""

from __future__ import annotations

import time
from pathlib import Path

from benchmarks.stats import p50, p95
from dotclaw.journal.metrics_types import InitPerfMetrics
from dotclaw.journal.storage import build_run_meta


async def run(
    warmup: int = 3,
    repeat: int = 10,
    project_root: str | Path | None = None,
) -> tuple[InitPerfMetrics, "RunMeta"]:
    """运行初始化性能评测，返回 (InitPerfMetrics, RunMeta)。"""
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

    meta = build_run_meta(
        run_id=f"bench_init_perf_{time.strftime('%H%M%S')}",
        test_dataset="framework_perf",
        test_dataset_size=repeat,
    )

    perf = InitPerfMetrics(
        config_load_ms=p50(results.get("config_load", [])),
        config_load_p95_ms=p95(results.get("config_load", [])),
        llm_build_ms=p50(results.get("llm_build", [])),
        llm_build_p95_ms=p95(results.get("llm_build", [])),
        skill_scan_ms=p50(results.get("skill_scan", [])),
        skill_scan_p95_ms=p95(results.get("skill_scan", [])),
        tool_build_ms=p50(results.get("tool_build", [])),
        tool_build_p95_ms=p95(results.get("tool_build", [])),
        session_mgr_ms=p50(results.get("session_mgr", [])),
        session_mgr_p95_ms=p95(results.get("session_mgr", [])),
        prompt_builder_ms=p50(results.get("prompt_builder", [])),
        prompt_builder_p95_ms=p95(results.get("prompt_builder", [])),
        memory_build_ms=p50(results.get("memory_build", [])),
        memory_build_p95_ms=p95(results.get("memory_build", [])),
        agent_full_ms=p50(results.get("agent_full", [])),
        agent_full_p95_ms=p95(results.get("agent_full", [])),
    )
    return perf, meta


# ═══════════════════════════════════════════════════════════════════
# 测量
# ═══════════════════════════════════════════════════════════════════


async def _measure_one_iteration(root: Path) -> dict[str, float]:
    """测量一轮所有组件的初始化耗时。"""
    times: dict[str, float] = {}

    from dotclaw.config.settings import load_config
    from dotclaw.agent.factory import _build_llm, _build_prompt_builder, _build_memory
    from dotclaw.memory.store import SessionManager

    t0 = time.perf_counter()
    config = load_config(str(root / "config.yaml"))
    times["config_load"] = (time.perf_counter() - t0) * 1000

    t0 = time.perf_counter()
    llm = _build_llm(config, root)
    times["llm_build"] = (time.perf_counter() - t0) * 1000

    t0 = time.perf_counter()
    skill_registry = _scan_bench_skills(config, root)
    times["skill_scan"] = (time.perf_counter() - t0) * 1000

    t0 = time.perf_counter()
    tool_executor = _build_tools(config, skill_registry)
    times["tool_build"] = (time.perf_counter() - t0) * 1000

    t0 = time.perf_counter()
    session_mgr = SessionManager(config.session.directory)
    times["session_mgr"] = (time.perf_counter() - t0) * 1000

    t0 = time.perf_counter()
    prompt_builder = _build_prompt_builder()
    times["prompt_builder"] = (time.perf_counter() - t0) * 1000

    t0 = time.perf_counter()
    await _build_memory(config, llm, root)
    times["memory_build"] = (time.perf_counter() - t0) * 1000

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

    return Agent(
        agent_config=agent_config, config=config, llm=llm,
        session_mgr=session_mgr, channel=None,
        tool_executor=tool_executor, prompt_builder=None,
        memory_mgr=mem_mgr, skill_registry=skill_registry,
        mcp_provider=None, memory_dream=mem_dream,
        mcp_task=None,
    )


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
        v50 = p50(durs); v95 = p95(durs); mn = min(durs); mx = max(durs)
        print(f"  {label:<{w}} {v50:>7.1f} {v95:>7.1f} {mn:>7.1f} {mx:>7.1f}")
        if key != "agent_full":
            total_p50 += v50
    print(f"  {'-'*w} {'-'*8} {'-'*8} {'-'*8} {'-'*8}")
    agent_p50 = p50(results.get("agent_full", []))
    print(f"  {'TOTAL (sum)':<{w}} {total_p50:>7.1f}")
    print(f"  {'Agent (measured)':<{w}} {agent_p50:>7.1f}")


def describe(perf: InitPerfMetrics) -> str:
    """返回该 case 的 Markdown 详情。"""
    rows = [
        f"| Config Load | {perf.config_load_ms:.1f} ms | {perf.config_load_p95_ms:.1f} ms |",
        f"| LLMProxy | {perf.llm_build_ms:.1f} ms | {perf.llm_build_p95_ms:.1f} ms |",
        f"| SkillRegistry (100) | {perf.skill_scan_ms:.1f} ms | {perf.skill_scan_p95_ms:.1f} ms |",
        f"| ToolExecutor | {perf.tool_build_ms:.1f} ms | {perf.tool_build_p95_ms:.1f} ms |",
        f"| SessionManager | {perf.session_mgr_ms:.1f} ms | {perf.session_mgr_p95_ms:.1f} ms |",
        f"| PromptBuilder | {perf.prompt_builder_ms:.1f} ms | {perf.prompt_builder_p95_ms:.1f} ms |",
        f"| MemoryManager | {perf.memory_build_ms:.1f} ms | {perf.memory_build_p95_ms:.1f} ms |",
        f"| **Agent Total** | **{perf.agent_full_ms:.1f} ms** | **{perf.agent_full_p95_ms:.1f} ms** |",
    ]
    return "\n".join([
        "| Component | P50 | P95 |",
        "|-----------|-----|-----|",
    ] + rows)
