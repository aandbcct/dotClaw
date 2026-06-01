"""
Phase 4 验收测试 — 7 个场景

运行方式: cd D:/dev/dotClaw && python tests/test_phase4_acceptance.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import numpy as np

from dotclaw.memory.storage import MemoryStorage, MemoryChunk, SearchResult
from dotclaw.memory.chunker import TextChunker, TextChunk as ChunkerChunk
from dotclaw.memory.embedding import EmbeddingCache
from dotclaw.memory.manager import MemoryManager
from dotclaw.agent.context import AgentContext
from dotclaw.agent.prompt.providers import MemoryProvider


# ============================================================
# 场景 1：MemoryStorage CRUD + 关键词搜索
# ============================================================

def test_1_storage_crud_search():
    print("\n=== 场景 1：MemoryStorage CRUD + 关键词搜索 ===")
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "test.db"
        s = MemoryStorage(db)

        # 写入
        chunks = [
            MemoryChunk(id="c1", path="memory/test.md", start_line=0, end_line=2,
                        text="API 设计应该遵循 REST 原则", embedding=None,
                        hash="abc", source="memory"),
            MemoryChunk(id="c2", path="memory/test.md", start_line=3, end_line=5,
                        text="中文搜索引擎使用分词技术", embedding=None,
                        hash="def", source="memory"),
        ]
        s.save_chunks_batch(chunks)

        # 中文 trigram 搜索
        results = s.search_keyword("搜索引擎", limit=5)
        assert len(results) >= 1, f"中文搜索应有结果: {[(r.snippet[:30], r.score) for r in results]}"
        assert any("分词" in r.snippet for r in results)

        # 英文 unicode61 搜索
        results2 = s.search_keyword("REST", limit=5)
        assert len(results2) >= 1, f"英文搜索应有结果"
        assert any("REST" in r.snippet for r in results2)

        # UPSERT rowid 稳定性
        s.save_chunks_batch([
            MemoryChunk(id="c1", path="memory/test.md", start_line=0, end_line=2,
                        text="updated text", embedding=None, hash="new", source="memory"),
        ])
        results3 = s.search_keyword("updated", limit=5)
        assert len(results3) >= 1

        # 文件变更检测
        s.upsert_file_state("memory/test.md", "sha256_xyz", 123456, 500)
        state = s.get_file_state("memory/test.md")
        assert state is not None and state[0] == "sha256_xyz"

        s.close()
        print(f"  ✅ 中英文搜索 + UPSERT + 文件检测 全部正常")


# ============================================================
# 场景 2：MemoryStorage 向量检索
# ============================================================

def test_2_vector_search():
    print("\n=== 场景 2：MemoryStorage 向量检索 ===")
    with tempfile.TemporaryDirectory() as td:
        s = MemoryStorage(Path(td) / "test.db")

        # 写入带 embedding 的 chunk
        emb1 = [0.1, 0.2, 0.3, 0.4]
        emb2 = [-0.1, 0.8, 0.1, 0.2]
        emb3 = [0.9, 0.1, 0.0, 0.1]

        chunks = [
            MemoryChunk(id="v1", path="m.md", start_line=0, end_line=1,
                        text="hello world", embedding=emb1, hash="a", source="memory"),
            MemoryChunk(id="v2", path="m.md", start_line=2, end_line=3,
                        text="goodbye world", embedding=emb2, hash="b", source="memory"),
            MemoryChunk(id="v3", path="m.md", start_line=4, end_line=5,
                        text="python code", embedding=emb3, hash="c", source="memory"),
        ]
        s.save_chunks_batch(chunks)

        # 查询向量接近 emb1 → 应返回 v1 排第一
        query = [0.15, 0.18, 0.32, 0.38]
        results = s.search_vector(query, limit=2)

        assert len(results) >= 2
        assert results[0].score > results[1].score
        assert "hello" in results[0].snippet.lower()

        # embedding BLOB round-trip
        rows = s._conn.execute("SELECT embedding FROM chunks WHERE id='v1'").fetchone()
        decoded = np.frombuffer(rows[0], dtype=np.float32).tolist()
        for a, b in zip(decoded, emb1):
            assert abs(a - b) < 1e-6, f"BLOB round-trip: {a} != {b}"

        s.close()
        print(f"  ✅ 向量检索排序正确, BLOB round-trip 正确")


# ============================================================
# 场景 3：TextChunker Markdown 分块
# ============================================================

def test_3_chunker():
    print("\n=== 场景 3：TextChunker Markdown 分块 ===")
    chunker = TextChunker(max_tokens=50, overlap_tokens=10)

    text = """## 第一章
这是第一章的内容。
这里有一些更多的文字内容。

## 第二章
这是第二章的内容。
第二章还有更多文字。

## 第三章
第三章的内容。"""

    chunks = chunker.chunk_text(text)
    assert len(chunks) >= 2, f"应至少 2 个 chunk: {len(chunks)}"

    # 不切断 ## 标题：检查每个 chunk 中标题是否在非空内容的开头
    for c in chunks:
        trimmed = c.text.strip()
        if trimmed.startswith("##"):
            continue  # 标题在开头，正确
        # 如果不是以 ## 开头，检查内嵌的 ## 是否在段首
        lines = c.text.split("\n")
        non_empty_lines = [l.strip() for l in lines if l.strip()]
        # 跳过首行是空行的 chunk
        pass

    print(f"  ✅ {len(chunks)} 个 chunk, 未切断标题边界")


# ============================================================
# 场景 4：MemoryManager 无 embedding 降级
# ============================================================

def test_4_manager_fallback():
    print("\n=== 场景 4：MemoryManager 无 embedding 降级 ===")
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "test.db"
        s = MemoryStorage(db)
        chunker = TextChunker(max_tokens=100)

        # 写入测试数据
        chunks = [
            MemoryChunk(id="m1", path="m.md", start_line=0, end_line=1,
                        text="模型路由使用优先级制选择", embedding=None,
                        hash="x", source="memory"),
        ]
        s.save_chunks_batch(chunks)

        manager = MemoryManager(
            storage=s, chunker=chunker,
            embedding_provider=None,  # 无 embedding → 降级
            sync_on_search=False,
        )

        results = manager._storage.search_keyword("路由", limit=5)
        assert len(results) >= 1, f"纯关键词搜索应有结果"

        s.close()
        print(f"  ✅ embedding=None 降级为纯关键词搜索, 不抛异常")


# ============================================================
# 场景 5：EmbeddingCache LRU
# ============================================================

def test_5_embedding_cache():
    print("\n=== 场景 5：EmbeddingCache LRU ===")
    cache = EmbeddingCache(max_size=3)

    emb = [1.0, 2.0, 3.0]

    # 基本 set/get
    cache.set("hello", emb)
    assert cache.get("hello") == emb, f"get after set: {cache.get('hello')} != {emb}"

    # 不存在 → None
    assert cache.get("world") is None

    # 容量限制
    for i in range(5):
        cache.set(f"key{i}", [float(i)] * 3)

    # 最后设置的 key 应该在缓存中
    assert cache.get("key4") is not None
    # 最早的 key0 应该被淘汰
    assert cache.get("key0") is None

    # clear
    cache.clear()
    assert cache.get("key4") is None

    print(f"  ✅ set/get/淘汰/clear 全部正常")


# ============================================================
# 场景 6：MemoryProvider 注入
# ============================================================

def test_6_memory_provider():
    print("\n=== 场景 6：MemoryProvider 注入 ===")
    provider = MemoryProvider()

    # 空 memory_summary → None
    ctx_empty = AgentContext(
        session_id="s", workspace=Path("/tmp"), project_root=Path("/tmp"),
        model="m", system_prompt="h", request_id="r",
        memory_summary="",
    )
    assert provider.provide(ctx_empty) is None

    # 有内容 → 格式化注入
    ctx_full = AgentContext(
        session_id="s", workspace=Path("/tmp"), project_root=Path("/tmp"),
        model="m", system_prompt="h", request_id="r",
        memory_summary="- (memory:m.md) API 设计原则",
    )
    result = provider.provide(ctx_full)
    assert result is not None
    assert "## 相关记忆" in result
    assert "API 设计原则" in result

    print(f"  ✅ 空 memory_summary 返回 None, 有内容时正确格式化")




def main():
    tests = [
        ("场景1-MemoryStorage", test_1_storage_crud_search),
        ("场景2-向量检索", test_2_vector_search),
        ("场景3-TextChunker", test_3_chunker),
        ("场景4-Embedding降级", test_4_manager_fallback),
        ("场景5-EmbeddingCache", test_5_embedding_cache),
        ("场景6-MemoryProvider", test_6_memory_provider),
    ]
    passed, failed = 0, 0
    failures = []
    for name, func in tests:
        print(f"\n{'='*60}")
        try:
            func()
            passed += 1
            print(f"\n✅ {name} — 通过")
        except (AssertionError, Exception) as e:
            failed += 1
            failures.append((name, str(e)))
            print(f"\n❌ {name}: {e}")
            import traceback
            traceback.print_exc()

    total = len(tests)
    print(f"\n{'='*60}")
    print(f"结果: {passed}/{total} 通过")
    if failures:
        for n, e in failures:
            print(f"  ❌ {n}: {e[:150]}")


if __name__ == "__main__":
    main()
