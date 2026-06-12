"""MemoryManager — 统一记忆检索入口 + sync 调度 + flush/dream 触发"""

from __future__ import annotations

import hashlib
import logging
import math
import time
from pathlib import Path
from typing import Any, TYPE_CHECKING

from .storage import MemoryStorage, MemoryChunk, SearchResult

if TYPE_CHECKING:
    from .chunker import TextChunker
    from .embedding import EmbeddingProvider, EmbeddingCache
    from .flush import MemoryFlushManager
    from ..memory.store import SessionMessage

logger = logging.getLogger("dotclaw.memory.manager")


class MemoryManager:
    """统一记忆检索入口"""

    def __init__(
        self,
        storage: MemoryStorage,
        chunker: "TextChunker",
        workspace: Path | None = None,
        embedding_provider: "EmbeddingProvider | None" = None,
        flush_manager: "MemoryFlushManager | None" = None,
        embedding_cache: "EmbeddingCache | None" = None,
        sync_on_search: bool = True,
        vector_weight: float = 0.7,
        keyword_weight: float = 0.3,
        max_results: int = 5,
        min_score: float = 0.1,
        temporal_decay_half_life_days: float = 30.0,
    ):
        self._storage = storage
        self._chunker = chunker
        self._embedding = embedding_provider
        self._flush_mgr = flush_manager
        self._cache = embedding_cache
        self._sync_on_search = sync_on_search
        self._vector_weight = vector_weight
        self._keyword_weight = keyword_weight
        self._max_results = max_results
        self._min_score = min_score
        self._half_life = temporal_decay_half_life_days
        self._memory_dir = (workspace or Path(".")) / "memory"
        self._syncing = False  # sync 递归防护

    async def search(
        self, query: str, max_results: int = 5, min_score: float = 0.1,
    ) -> list[SearchResult]:
        """混合检索：向量 + FTS5 + 时间衰减"""
        if self._sync_on_search:
            await self.sync()

        vector_results: list[SearchResult] = []
        keyword_results: list[SearchResult] = []

        if self._embedding:
            embedding = self._get_embedding(query)
            if embedding:
                vector_results = self._storage.search_vector(embedding, max_results * 2)

        keyword_results = self._storage.search_keyword(query, max_results * 2)

        # 时间衰减
        vector_results = self._apply_temporal_decay(vector_results)
        keyword_results = self._apply_temporal_decay(keyword_results)

        # 加权合并
        merged: dict[str, SearchResult] = {}
        for r in vector_results:
            key = f"{r.path}:{r.start_line}"
            r.score = r.score * self._vector_weight
            merged[key] = r

        for r in keyword_results:
            key = f"{r.path}:{r.start_line}"
            kw_score = (1.0 - abs(r.score)) * self._keyword_weight if r.score < -0.1 else r.score * self._keyword_weight
            if key in merged:
                merged[key].score += kw_score
            else:
                r.score = kw_score
                merged[key] = r

        results = sorted(merged.values(), key=lambda r: r.score, reverse=True)
        return [r for r in results if r.score >= min_score][:max_results]

    async def sync(self, force: bool = False):
        """文件变更检测 → 分块 → 批量 embedding → 写入索引"""
        import asyncio
        import hashlib

        # 递归防护
        if self._syncing:
            return
        self._syncing = True
        try:
            # 监控的文件列表
            monitored = [
                self._memory_dir / "MEMORY.md",
            ]
            # 也扫描 skills 目录下的知识文件
            skills_knowledge = self._memory_dir.parent.parent / "skills" / "knowledge"
            if skills_knowledge.exists():
                for f in skills_knowledge.glob("*.md"):
                    monitored.append(f)

            for file_path in monitored:
                if not file_path.exists():
                    continue

                # 计算文件 hash
                content = file_path.read_text(encoding="utf-8")
                file_hash = hashlib.sha256(content.encode()).hexdigest()
                mtime = int(file_path.stat().st_mtime)
                size = file_path.stat().st_size

                # 检查是否需要更新
                existing = self._storage.get_file_state(str(file_path))
                if not force and existing and existing[0] == file_hash:
                    continue

                # 分块
                chunks = self._chunker.chunk_text(content)
                rel_path = str(file_path.relative_to(self._memory_dir.parent.parent))

                # 生成 ID
                memory_chunks = []
                for i, c in enumerate(chunks):
                    chunk_id = hashlib.sha256(f"{rel_path}:{c.start_line}:{c.end_line}".encode()).hexdigest()[:16]
                    memory_chunks.append(MemoryChunk(
                        id=chunk_id,
                        path=rel_path,
                        start_line=c.start_line,
                        end_line=c.end_line,
                        text=c.text,
                        embedding=None,
                        hash=hashlib.sha256(c.text.encode()).hexdigest()[:16],
                        source="memory" if "MEMORY.md" in str(file_path) else "knowledge",
                    ))

                # batch embedding
                if self._embedding and memory_chunks:
                    try:
                        embeddings = await asyncio.to_thread(
                            self._embedding.embed_batch, [c.text for c in memory_chunks]
                        )
                        for c, emb in zip(memory_chunks, embeddings):
                            c.embedding = emb
                    except Exception as e:
                        logger.warning(f"Embedding 生成失败，跳过向量索引: {e}")

                # 写入存储
                self._storage.delete_by_path(rel_path)
                self._storage.save_chunks_batch(memory_chunks)
                self._storage.upsert_file_state(rel_path, file_hash, mtime, size)
                logger.info(f"已同步: {rel_path} ({len(memory_chunks)} chunks)")
        finally:
            self._syncing = False

    async def flush_memory(
        self, messages: list, reason: str = "threshold",
        journal: Any | None = None,
    ) -> bool:
        """触发 L2 日记忆写入"""
        success = False
        if self._flush_mgr:
            success = await self._flush_mgr.flush_from_messages(
                messages=messages, reason=reason
            )
            if journal:
                journal.memory_write("daily_note", "success" if success else "error")
        return success

    def _get_embedding(self, text: str) -> list[float] | None:
        if not self._embedding:
            return None
        if self._cache:
            cached = self._cache.get(text)
            if cached is not None:
                return cached
        emb = self._embedding.embed_query(text)
        if self._cache and emb:
            self._cache.set(text, emb)
        return emb

    def _apply_temporal_decay(self, results: list[SearchResult]) -> list[SearchResult]:
        """对日记忆文件应用半衰期衰减（MEMORY.md 不衰减）"""
        if self._half_life <= 0:
            return results

        now = time.time()
        half_life_seconds = self._half_life * 86400  # 天 → 秒

        for r in results:
            if r.source != "memory":
                continue
            # 从文件名提取日期
            try:
                date_str = Path(r.path).stem  # YYYY-MM-DD
                from datetime import datetime
                dt = datetime.strptime(date_str, "%Y-%m-%d")
                age_seconds = now - dt.timestamp()
                decay = math.exp(-age_seconds * math.log(2) / half_life_seconds)
                r.score *= decay
            except (ValueError, OSError):
                pass
        return results

    @staticmethod
    def _hash_content(content: str) -> str:
        return hashlib.sha256(content.encode()).hexdigest()
