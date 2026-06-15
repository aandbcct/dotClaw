"""benchmarks/cases/memory_perf.py — 记忆检索性能评测。

测量 MemoryStorage FTS5 搜索在不同 index_size（100/1000/10000 chunks）下的
P50/P95 延迟。
"""

from __future__ import annotations

import tempfile
import time
import uuid
from pathlib import Path

from dotclaw.journal.metrics_types import AgentRunSnapshot
from dotclaw.journal.storage import build_run_meta


async def run(
    warmup: int = 3,
    repeat: int = 10,
    project_root: str | Path | None = None,
) -> AgentRunSnapshot:
    """运行记忆检索性能评测。

    测量 FTS5 关键词检索在不同索引大小下的延迟。
    """
    root = Path(project_root) if project_root else Path(__file__).parent.parent.parent
    corpus_dir = root / "benchmarks" / "dataset" / "memory_corpus"

    sizes = {
        "small": 100,
        "medium": 1000,
        "large": 10000,
    }

    results: dict[str, list[float]] = {}

    for name, chunk_count in sizes.items():
        corpus_file = corpus_dir / f"{name}.txt"
        if not corpus_file.exists():
            print(f"  [SKIP] Corpus file not found: {corpus_file}")
            continue

        print(f"\n  --- {name} ({chunk_count} chunks) ---")

        # 预填充一次
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
            db_path = Path(tmp.name)

        try:
            chunks = _load_chunks(corpus_file, chunk_count)
            storage = _create_storage(db_path)
            _insert_chunks(storage, chunks)
            db_size_kb = db_path.stat().st_size / 1024

            durs = []
            for i in range(warmup + repeat):
                query = _pick_query(chunks, i)
                t0 = time.perf_counter()
                results_found = storage.search_keyword(query, 10)
                dt = (time.perf_counter() - t0) * 1000

                if i >= warmup:
                    durs.append(dt)

            results[name] = durs
            _print_stats(name, chunk_count, db_size_kb, durs)
        finally:
            try:
                storage.close()
            except Exception:
                pass
            try:
                db_path.unlink(missing_ok=True)
            except (PermissionError, OSError):
                pass

    # ── 构建 Snapshot ──
    meta = build_run_meta(
        run_id=f"bench_memory_perf_{time.strftime('%H%M%S')}",
        test_dataset="framework_perf",
        test_dataset_size=repeat,
    )

    from dotclaw.journal.metrics_types import (
        AgentGeneralMetrics, ReactLoopMetrics,
        ToolCallMetrics, SkillMetrics, MemoryMetrics,
    )

    large_durs = results.get("large", [])

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
        memory=MemoryMetrics(
            total_retrievals=repeat * len(sizes),
            avg_retrieval_ms=_p50(large_durs) if large_durs else 0.0,
            p95_retrieval_ms=_p95(large_durs) if large_durs else 0.0,
        ),
        general=AgentGeneralMetrics(
            avg_e2e_latency_ms=_p50(large_durs) if large_durs else 0.0,
            p95_e2e_latency_ms=_p95(large_durs) if large_durs else 0.0,
        ),
    )
    return snapshot


# ═══════════════════════════════════════════════════════════════════
# 辅助
# ═══════════════════════════════════════════════════════════════════


def _load_chunks(path: Path, count: int) -> list[str]:
    """从语料文件加载前 count 行作为 chunks。"""
    chunks = []
    with open(path, encoding="utf-8") as f:
        for i, line in enumerate(f):
            if i >= count:
                break
            line = line.strip()
            if line:
                chunks.append(line)
    return chunks


def _create_storage(db_path: Path) -> "MemoryStorage":
    """创建 MemoryStorage 实例。"""
    from dotclaw.memory.storage import MemoryStorage
    return MemoryStorage(db_path)


def _insert_chunks(storage: "MemoryStorage", chunks: list[str]) -> None:
    """向 storage 插入 chunks。"""
    from dotclaw.memory.storage import MemoryChunk
    mc_list = []
    for i, text in enumerate(chunks):
        mc_list.append(MemoryChunk(
            id=str(uuid.uuid4()),
            path=f"/bench/memory/{i:05d}.txt",
            start_line=0,
            end_line=1,
            text=text,
            embedding=None,
            hash=str(hash(text)),
            source="benchmark",
        ))
    storage.save_chunks_batch(mc_list)


def _pick_query(chunks: list[str], seed: int) -> str:
    """从 chunks 中选择一个搜索词。"""
    if not chunks:
        return "benchmark test query"
    # 取某个 chunk 中间的几个词
    idx = seed % len(chunks)
    words = chunks[idx].split()[:5]
    return " ".join(words) if words else "benchmark"


def _print_stats(name: str, count: int, db_size_kb: float, durs: list[float]) -> None:
    """打印统计信息。"""
    if not durs:
        return
    print(f"  DB size: {db_size_kb:.0f} KB")
    print(f"  P50: {_p50(durs):.2f}ms  P95: {_p95(durs):.2f}ms  "
          f"Avg: {sum(durs)/len(durs):.2f}ms  "
          f"Min: {min(durs):.2f}ms  Max: {max(durs):.2f}ms")


def _p50(values: list[float]) -> float:
    if not values: return 0.0
    s = sorted(values)
    return s[int(len(s) * 0.50)]


def _p95(values: list[float]) -> float:
    if not values: return 0.0
    s = sorted(values)
    return s[int(len(s) * 0.95)]
