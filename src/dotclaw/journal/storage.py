"""快照存储：JSON 序列化、文件读写、简易 diff 对比。

提供：
- snapshot_to_json() / snapshot_from_json() — JSON 往返
- save_snapshot() / load_snapshot() — 文件 I/O
- diff_snapshots(baseline, candidate) — 对比两份快照
"""

from __future__ import annotations

import dataclasses
import json
import logging
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any

from dotclaw.journal.metrics_types import (
    AgentGeneralMetrics,
    AgentRunSnapshot,
    MemoryMetrics,
    ReactLoopMetrics,
    RunMeta,
    SkillMetrics,
    ToolCallMetrics,
)

logger = logging.getLogger("dotclaw.journal.storage")


# =============================================================================
# JSON 序列化
# =============================================================================


def snapshot_to_json(snapshot: AgentRunSnapshot, indent: int = 2) -> str:
    """快照 → 格式化 JSON 字符串。"""
    d = dataclasses.asdict(snapshot)
    return json.dumps(d, ensure_ascii=False, indent=indent)


def snapshot_from_json(json_str: str) -> AgentRunSnapshot:
    """JSON 字符串 → AgentRunSnapshot。"""
    d = json.loads(json_str)
    react = ReactLoopMetrics(**d.get("react", {}))
    tools = ToolCallMetrics(**d.get("tools", {}))
    skills = SkillMetrics(**d.get("skills", {}))
    memory = MemoryMetrics(**d.get("memory", {}))
    general = AgentGeneralMetrics(**d.get("general", {}))

    return AgentRunSnapshot(
        run_id=d.get("run_id", ""),
        timestamp=d.get("timestamp", ""),
        git_commit=d.get("git_commit", ""),
        config_hash=d.get("config_hash", ""),
        test_dataset=d.get("test_dataset", ""),
        test_dataset_size=d.get("test_dataset_size", 0),
        react=react,
        tools=tools,
        skills=skills,
        memory=memory,
        general=general,
    )


def _ensure_dir(path: Path) -> None:
    """确保目录存在。"""
    path.mkdir(parents=True, exist_ok=True)


def save_snapshot(snapshot: AgentRunSnapshot, directory: str | Path | None = None) -> Path:
    """保存快照到文件。

    Args:
        snapshot: AgentRunSnapshot 实例。
        directory: 输出目录，默认 "data/snapshots/"。

    Returns:
        写入的文件路径。
    """
    out_dir = Path(directory) if directory else Path("data/snapshots")
    _ensure_dir(out_dir)

    filename = f"{snapshot.run_id}.json"
    filepath = out_dir / filename

    json_str = snapshot_to_json(snapshot)
    filepath.write_text(json_str, encoding="utf-8")
    logger.info(f"快照已保存: {filepath}")
    return filepath


def load_snapshot(filepath: str | Path) -> AgentRunSnapshot:
    """从文件加载快照。"""
    content = Path(filepath).read_text(encoding="utf-8")
    return snapshot_from_json(content)


# =============================================================================
# RunMeta 辅助
# =============================================================================


def _get_git_commit() -> str:
    """获取当前 HEAD commit hash，失败返回 "unknown"。"""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return "unknown"


def _build_config_hash(config_path: str | Path, router_config_path: str | Path) -> str:
    """从配置文件计算 config_hash。

    SHA256(config.yaml + model_router_config.yaml)，失败返回 "unknown"。
    """
    import hashlib
    try:
        h = hashlib.sha256()
        has_data = False
        for cfg_path in (config_path, router_config_path):
            p = Path(cfg_path)
            if p.exists():
                h.update(p.read_bytes())
                has_data = True
        if not has_data:
            return "unknown"
        return h.hexdigest()[:16]
    except Exception:
        return "unknown"


def build_run_meta(
    run_id: str,
    test_dataset: str,
    test_dataset_size: int,
    config_path: str | Path = "config.yaml",
    router_config_path: str | Path = "model_router_config.yaml",
) -> RunMeta:
    """构建 RunMeta，自动填充 git_commit、config_hash 和 timestamp。

    Args:
        run_id: 运行标识（如 "run_20260607_001"）。
        test_dataset: 测试数据集名称（如 "bench_v1"）。
        test_dataset_size: 测试样本数。
        config_path: 配置文件路径。
        router_config_path: 路由配置路径。

    Returns:
        填充完整的 RunMeta。
    """
    from datetime import datetime, timezone

    return RunMeta(
        run_id=run_id,
        timestamp=datetime.now(timezone.utc).isoformat(),
        git_commit=_get_git_commit(),
        config_hash=_build_config_hash(config_path, router_config_path),
        test_dataset=test_dataset,
        test_dataset_size=test_dataset_size,
    )


# =============================================================================
# 快照对比
# =============================================================================


def _is_improvement(field_name: str, pct: float) -> bool:
    """判断指标变化是改善还是退化。

    优先匹配显式字段名（避免 rate 子串误判 empty_action_rate 等），
    其次通过关键词启发式判断。
    """
    # 显式字段：含 rate 但实际越低越好
    _LOWER_IS_BETTER_EXPLICIT = {
        "empty_action_rate", "redundant_action_rate", "retry_rate",
        "write_failures", "context_overflow_count",
    }
    fname = field_name.lower()
    for suffix in _LOWER_IS_BETTER_EXPLICIT:
        if suffix in fname:
            return pct < -2.0

    higher_is_better = {"rate", "accuracy", "precision", "recall", "hit_rate", "success"}
    lower_is_better = {"duration", "latency", "ms", "tokens", "cost", "loops", "errors",
                       "usd", "overhead", "overflow", "failures", "tps", "ttft"}

    if any(k in fname for k in higher_is_better):
        return pct > 2.0
    if any(k in fname for k in lower_is_better):
        return pct < -2.0
    return False


def _flatten_snapshot(snapshot: AgentRunSnapshot) -> dict[str, float | int]:
    """将快照展平为扁平标量字典（仅数值类型）。

    展开 react/tools/skills/memory/general 五个 section 下的直接标量字段，
    用 "." 连接路径。dict 嵌套值（如 calls_by_tool）不展开。
    """
    result: dict[str, float | int] = {}
    d = dataclasses.asdict(snapshot)

    for section_name in ("react", "tools", "skills", "memory", "general"):
        section = d.get(section_name, {})
        for key, value in section.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                result[f"{section_name}.{key}"] = value
    return result


def diff_snapshots(
    baseline: AgentRunSnapshot,
    candidate: AgentRunSnapshot,
    threshold: float = 2.0,
) -> list[str]:
    """对比两份快照，返回人类可读的差异行列表。

    对每个标量字段计算变化百分比，标注改善/退化。
    变化幅度 < threshold% 的字段跳过。
    baseline 为 0 的字段标注 "N/A"。

    Args:
        baseline: 基线快照（优化前）。
        candidate: 候选快照（优化后）。
        threshold: 变化阈值（百分比），低于此值不输出。

    Returns:
        差异描述行列表，格式如:
        ["react.task_completion_rate: 0.85 -> 0.92 (+8.2%) ✅",
         "react.avg_loop_duration_ms: 5000 -> 8000 (+60.0%) ❌"]
    """
    baseline_flat = _flatten_snapshot(baseline)
    candidate_flat = _flatten_snapshot(candidate)

    lines: list[str] = []

    all_keys = sorted(set(baseline_flat.keys()) | set(candidate_flat.keys()))
    for key in all_keys:
        base_val = baseline_flat.get(key, 0)
        cand_val = candidate_flat.get(key, 0)

        if base_val == 0:
            # baseline is 0 — report new appearances
            if cand_val != 0:
                lines.append(f"{key}: N/A → {cand_val} (new)")
            continue

        pct = (cand_val - base_val) / base_val * 100
        if abs(pct) < threshold:
            continue

        improved = _is_improvement(key, pct)
        marker = "✅" if improved else "❌"
        lines.append(f"{key}: {base_val} -> {cand_val} ({pct:+.1f}%) {marker}")

    return lines
