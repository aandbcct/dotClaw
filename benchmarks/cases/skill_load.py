"""benchmarks/cases/skill_load.py — Skill 加载性能评测。

测量 SkillScanner + SkillRegistry 在不同 skill 数量下的扫描耗时。
"""

from __future__ import annotations

import shutil
import tempfile
import time
from pathlib import Path

from dotclaw.journal.metrics_types import AgentRunSnapshot
from dotclaw.journal.storage import build_run_meta


async def run(
    warmup: int = 3,
    repeat: int = 10,
    project_root: str | Path | None = None,
) -> AgentRunSnapshot:
    """运行 Skill 加载性能评测。

    测量 10/50/100 个 skills 目录下的扫描和注册耗时。

    Args:
        warmup: 前 N 次迭代丢弃。
        repeat: 实际测量迭代次数。
        project_root: 项目根目录。

    Returns:
        AgentRunSnapshot。
    """
    root = Path(project_root) if project_root else Path(__file__).parent.parent.parent
    dataset_dir = root / "benchmarks" / "dataset" / "sample_skills"

    sizes = [10, 50, 100]
    results: dict[int, dict[str, list[float]]] = {}

    for count in sizes:
        print(f"\n  --- {count} skills ---")
        scan_times, body_times = [], []

        for i in range(warmup + repeat):
            # 创建临时目录，只复制 count 个 skill
            with tempfile.TemporaryDirectory() as tmpdir:
                _copy_skills_subset(dataset_dir, Path(tmpdir), count)

                # 测量 scan + register
                t0 = time.perf_counter()
                registry = _scan_skills([str(Path(tmpdir))])
                dt_scan = (time.perf_counter() - t0) * 1000

                # 测量 body 加载（get_descriptions_block）
                t0 = time.perf_counter()
                _ = registry.get_descriptions_block()
                dt_body = (time.perf_counter() - t0) * 1000

                if i >= warmup:
                    scan_times.append(dt_scan)
                    body_times.append(dt_body)

        results[count] = {"scan": scan_times, "body": body_times}
        _print_size_stats(count, scan_times, body_times)

    # ── 构建 Snapshot ──
    meta = build_run_meta(
        run_id=f"bench_skill_load_{time.strftime('%H%M%S')}",
        test_dataset="framework_perf",
        test_dataset_size=repeat,
    )

    from dotclaw.journal.metrics_types import (
        AgentGeneralMetrics, ReactLoopMetrics,
        ToolCallMetrics, SkillMetrics, MemoryMetrics,
    )

    # 使用 100 skills 的数据填充 SkillMetrics
    d100 = results.get(100, {})
    scan_durs = d100.get("scan", [])
    body_durs = d100.get("body", [])

    snapshot = AgentRunSnapshot(
        run_id=meta.run_id,
        timestamp=meta.timestamp,
        git_commit=meta.git_commit,
        config_hash=meta.config_hash,
        test_dataset=meta.test_dataset,
        test_dataset_size=meta.test_dataset_size,
        react=ReactLoopMetrics(),
        tools=ToolCallMetrics(),
        skills=SkillMetrics(
            total_triggers=100 * repeat,  # 每轮扫描 100 个 skill
            avg_body_load_ms=_p50(body_durs) if body_durs else 0.0,
        ),
        memory=MemoryMetrics(),
        general=AgentGeneralMetrics(
            avg_e2e_latency_ms=_p50(scan_durs) if scan_durs else 0.0,
            p95_e2e_latency_ms=_p95(scan_durs) if scan_durs else 0.0,
        ),
    )
    return snapshot


# ═══════════════════════════════════════════════════════════════════
# 辅助
# ═══════════════════════════════════════════════════════════════════


def _copy_skills_subset(src: Path, dst: Path, count: int) -> None:
    """复制前 count 个 skill 目录到 tmpdir。"""
    skill_dirs = sorted(
        [d for d in src.iterdir() if d.is_dir()],
        key=lambda x: x.name,
    )[:count]
    for sd in skill_dirs:
        target = dst / sd.name
        shutil.copytree(sd, target)


def _scan_skills(dirs: list[str]):
    """扫描指定目录下的所有 skills。"""
    from dotclaw.skills.scanner import SkillScanner
    from dotclaw.skills.registry import SkillRegistry

    scanner = SkillScanner(dirs)
    metas = scanner.scan()
    registry = SkillRegistry()
    for meta in metas:
        registry.register(meta)
    return registry


def _print_size_stats(count: int, scan_times: list[float], body_times: list[float]) -> None:
    """打印指定 size 下的统计。"""
    if not scan_times:
        return
    print(f"  Scan:  P50={_p50(scan_times):.1f}ms  P95={_p95(scan_times):.1f}ms  "
          f"Min={min(scan_times):.1f}ms  Max={max(scan_times):.1f}ms")
    if body_times:
        print(f"  Body:  P50={_p50(body_times):.1f}ms  P95={_p95(body_times):.1f}ms  "
              f"Min={min(body_times):.1f}ms  Max={max(body_times):.1f}ms")


def _p50(values: list[float]) -> float:
    if not values: return 0.0
    s = sorted(values)
    return s[int(len(s) * 0.50)]


def _p95(values: list[float]) -> float:
    if not values: return 0.0
    s = sorted(values)
    return s[int(len(s) * 0.95)]
