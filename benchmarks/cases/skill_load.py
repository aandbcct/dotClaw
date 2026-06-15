"""benchmarks/cases/skill_load.py — Skill 加载性能评测。

测量 SkillScanner + SkillRegistry 在不同 skill 数量下的扫描耗时。
返回 dict[int, SkillsMetrics]，按数量分组。
"""

from __future__ import annotations

import shutil
import tempfile
import time
from pathlib import Path

from benchmarks.stats import p50, p95
from dotclaw.journal.metrics_types import SkillMetrics
from dotclaw.journal.storage import build_run_meta


async def run(
    warmup: int = 3,
    repeat: int = 10,
    project_root: str | Path | None = None,
) -> tuple[dict[int, SkillMetrics], "RunMeta"]:
    """运行 Skill 加载性能评测，返回 (dict[int, SkillsMetrics], RunMeta)。"""
    root = Path(project_root) if project_root else Path(__file__).parent.parent.parent
    dataset_dir = root / "benchmarks" / "dataset" / "sample_skills"

    sizes = [10, 50, 100]
    by_count: dict[int, SkillsMetrics] = {}

    for count in sizes:
        print(f"\n  --- {count} skills ---")
        scan_times, body_times = [], []

        for i in range(warmup + repeat):
            with tempfile.TemporaryDirectory() as tmpdir:
                _copy_skills_subset(dataset_dir, Path(tmpdir), count)

                t0 = time.perf_counter()
                registry = _scan_skills([str(Path(tmpdir))])
                dt_scan = (time.perf_counter() - t0) * 1000

                t0 = time.perf_counter()
                _ = registry.get_descriptions_block()
                dt_body = (time.perf_counter() - t0) * 1000

                if i >= warmup:
                    scan_times.append(dt_scan)
                    body_times.append(dt_body)

        by_count[count] = SkillMetrics(
            total_triggers=count * repeat,
            avg_body_load_ms=p50(body_times),
        )
        _print_size_stats(count, scan_times, body_times)

    meta = build_run_meta(
        run_id=f"bench_skill_load_{time.strftime('%H%M%S')}",
        test_dataset="framework_perf",
        test_dataset_size=repeat,
    )
    return by_count, meta


def _copy_skills_subset(src: Path, dst: Path, count: int) -> None:
    skill_dirs = sorted(
        [d for d in src.iterdir() if d.is_dir()],
        key=lambda x: x.name,
    )[:count]
    for sd in skill_dirs:
        shutil.copytree(sd, dst / sd.name)


def _scan_skills(dirs: list[str]):
    from dotclaw.skills.scanner import SkillScanner
    from dotclaw.skills.registry import SkillRegistry
    scanner = SkillScanner(dirs)
    metas = scanner.scan()
    registry = SkillRegistry()
    for meta in metas:
        registry.register(meta)
    return registry


def _print_size_stats(count: int, scan_times: list[float], body_times: list[float]) -> None:
    if not scan_times:
        return
    print(f"  Scan:  P50={p50(scan_times):.1f}ms  P95={p95(scan_times):.1f}ms  "
          f"Min={min(scan_times):.1f}ms  Max={max(scan_times):.1f}ms")
    if body_times:
        print(f"  Body:  P50={p50(body_times):.1f}ms  P95={p95(body_times):.1f}ms  "
              f"Min={min(body_times):.1f}ms  Max={max(body_times):.1f}ms")


def describe(by_count: dict[int, SkillMetrics]) -> str:
    """返回该 case 的 Markdown 详情。"""
    rows = []
    for count in [10, 50, 100]:
        m = by_count.get(count)
        if m:
            rows.append(f"| {count} | {m.avg_body_load_ms:.1f} ms | {m.total_triggers} |")
    return "\n".join([
        "| Skills | Body Load P50 | Triggers |",
        "|--------|--------------|----------|",
    ] + rows)
