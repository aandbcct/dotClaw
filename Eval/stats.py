"""Eval/stats.py — 统计工具函数，所有 case 共享。"""

import dataclasses
import json
from pathlib import Path
from typing import Any

from dotclaw.journal.metrics_types import (
    AgentGeneralMetrics, AgentRunSnapshot,
    InitPerfMetrics, MemoryMetrics,
    ReactLoopMetrics, RunMeta, SkillMetrics, ToolCallMetrics,
)


def p50(values: list[float]) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    return s[int(len(s) * 0.50)]


def p95(values: list[float]) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    return s[int(len(s) * 0.95)]


def _make_snapshot(
    meta: RunMeta,
    react: ReactLoopMetrics | None = None,
    tools: ToolCallMetrics | None = None,
    skills: SkillMetrics | None = None,
    memory: MemoryMetrics | None = None,
    general: AgentGeneralMetrics | None = None,
) -> AgentRunSnapshot:
    return AgentRunSnapshot(
        meta=meta,
        react=react or ReactLoopMetrics(),
        tools=tools or ToolCallMetrics(),
        skills=skills or SkillMetrics(),
        memory=memory or MemoryMetrics(),
        general=general or AgentGeneralMetrics(),
    )


def build_bench_snapshot(metrics: Any, meta: Any) -> AgentRunSnapshot:
    """将 case 返回的 metrics + meta 转为 AgentRunSnapshot。"""
    if isinstance(metrics, AgentRunSnapshot):
        return metrics

    if isinstance(metrics, InitPerfMetrics):
        gen = AgentGeneralMetrics(
            avg_e2e_latency_ms=metrics.agent_full_ms,
            p95_e2e_latency_ms=metrics.agent_full_p95_ms,
        )
        return _make_snapshot(meta, general=gen)

    if isinstance(metrics, ToolCallMetrics):
        return _make_snapshot(meta, tools=metrics)

    if isinstance(metrics, AgentGeneralMetrics):
        return _make_snapshot(meta, general=metrics)

    if isinstance(metrics, dict):
        first_val = next(iter(metrics.values()), None) if metrics else None

        if isinstance(first_val, MemoryMetrics):
            large = metrics.get("large", MemoryMetrics())
            gen = AgentGeneralMetrics(
                avg_e2e_latency_ms=large.avg_retrieval_ms,
                p95_e2e_latency_ms=large.p95_retrieval_ms,
            )
            return _make_snapshot(meta, memory=large, general=gen)

        if isinstance(first_val, SkillMetrics):
            sk = metrics.get(100, SkillMetrics())
            gen = AgentGeneralMetrics(avg_e2e_latency_ms=sk.avg_body_load_ms)
            return _make_snapshot(meta, skills=sk, general=gen)

    return _make_snapshot(meta)


def _to_dict(obj: Any) -> Any:
    """递归将 dataclass + dict 转为纯 dict。"""
    if dataclasses.is_dataclass(obj):
        return dataclasses.asdict(obj)
    if isinstance(obj, dict):
        return {str(k): _to_dict(v) for k, v in obj.items()}
    return obj


def save_case_result(metrics: Any, meta: RunMeta, directory: str | Path) -> Path:
    """将 case 的 metrics + meta 写入 JSON 快照文件。

    不转换为 AgentRunSnapshot，只包含该 case 自己的指标类型。
    """
    out_dir = Path(directory)
    out_dir.mkdir(parents=True, exist_ok=True)

    data = {
        "meta": dataclasses.asdict(meta),
        "metrics": _to_dict(metrics),
    }
    filename = f"{meta.run_id}.json"
    filepath = out_dir / filename
    filepath.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return filepath
