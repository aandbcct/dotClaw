"""benchmarks/cases/memory_perf.py — 记忆检索性能评测。

测量 MemoryStorage FTS5 搜索在不同 index_size（100/1000/10000 chunks）下的
P50/P95 延迟。返回 dict[str, MemoryMetrics]，按 size 分组。
"""

from __future__ import annotations

import tempfile
import time
import uuid
from pathlib import Path

from benchmarks.stats import p50, p95
from dotclaw.journal.metrics_types import MemoryMetrics
from dotclaw.journal.storage import build_run_meta


async def run(
    warmup: int = 3,
    repeat: int = 10,
    project_root: str | Path | None = None,
) -> tuple[dict[str, MemoryMetrics], "RunMeta"]:
    """运行记忆检索性能评测，返回 (dict[str, MemoryMetrics], RunMeta)。"""
    root = Path(project_root) if project_root else Path(__file__).parent.parent.parent
    corpus_dir = root / "benchmarks" / "dataset" / "memory_corpus"

    sizes = {"small": 100, "medium": 1000, "large": 10000}
    by_size: dict[str, MemoryMetrics] = {}

    for name, chunk_count in sizes.items():
        corpus_file = corpus_dir / f"{name}.txt"
        if not corpus_file.exists():
            print(f"  [SKIP] Corpus file not found: {corpus_file}")
            continue

        print(f"\n  --- {name} ({chunk_count} chunks) ---")

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

            by_size[name] = MemoryMetrics(
                total_retrievals=repeat,
                avg_retrieval_ms=p50(durs),
                p95_retrieval_ms=p95(durs),
                index_size=chunk_count,
                index_size_mb=db_size_kb / 1024,
            )
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

    meta = build_run_meta(
        run_id=f"bench_memory_perf_{time.strftime('%H%M%S')}",
        test_dataset="framework_perf",
        test_dataset_size=repeat,
    )
    return by_size, meta


# ═══════════════════════════════════════════════════════════════════
# 辅助
# ═══════════════════════════════════════════════════════════════════


def _load_chunks(path: Path, count: int) -> list[str]:
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
    from dotclaw.memory.storage import MemoryStorage
    return MemoryStorage(db_path)


def _insert_chunks(storage: "MemoryStorage", chunks: list[str]) -> None:
    from dotclaw.memory.storage import MemoryChunk
    mc_list = []
    for i, text in enumerate(chunks):
        mc_list.append(MemoryChunk(
            id=str(uuid.uuid4()),
            path=f"/bench/memory/{i:05d}.txt",
            start_line=0, end_line=1,
            text=text, embedding=None,
            hash=str(hash(text)), source="benchmark",
        ))
    storage.save_chunks_batch(mc_list)


def _pick_query(chunks: list[str], seed: int) -> str:
    if not chunks:
        return "benchmark test query"
    idx = seed % len(chunks)
    words = chunks[idx].split()[:5]
    return " ".join(words) if words else "benchmark"


def _print_stats(name: str, count: int, db_size_kb: float, durs: list[float]) -> None:
    if not durs:
        return
    print(f"  DB size: {db_size_kb:.0f} KB")
    print(f"  P50: {p50(durs):.2f}ms  P95: {p95(durs):.2f}ms  "
          f"Avg: {sum(durs)/len(durs):.2f}ms  "
          f"Min: {min(durs):.2f}ms  Max: {max(durs):.2f}ms")


def describe(by_size: dict[str, MemoryMetrics]) -> str:
    """返回该 case 的 Markdown 详情。"""
    rows = []
    for name in ["small", "medium", "large"]:
        m = by_size.get(name)
        if m:
            rows.append(f"| {name} ({m.index_size} chunks) | {m.avg_retrieval_ms:.1f} ms | {m.p95_retrieval_ms:.1f} ms | {m.index_size_mb:.1f} MB |")
    return "\n".join([
        "| Size | P50 | P95 | DB Size |",
        "|------|-----|-----|---------|",
    ] + rows)
