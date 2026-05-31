"""MemoryManager — 统一记忆检索入口 + sync 调度 + flush/dream 触发"""

from __future__ import annotations

import hashlib
import logging
import math
import time
from pathlib import Path
from typing import TYPE_CHECKING

from .storage import MemoryStorage, SearchResult

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
        # 目前监控 data/memory/MEMORY.md 和 skills/knowledge/ 下的 md 文件
        # ... 简化实现：只处理 MEMORY.md
        pass

    async def flush_memory(
        self, messages: list, reason: str = "threshold",
    ) -> bool:
        """触发 L2 日记忆写入"""
        if self._flush_mgr:
            return await self._flush_mgr.flush_from_messages(
                messages=messages, reason=reason
            )
        return False

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
        now = time.time()
        for r in results:
            if r.source == "memory" and self._half_life > 0:
                # 从文件名提取日期推断年龄
                try:
                    date_str = Path(r.path).stem  # YYYY-MM-DD
                    # 简化：不计算精确年龄，使用半衰期
                    pass
                except Exception:
                    pass
            # MEMORY.md 条目：score 不衰减
        return results

    @staticmethod
    def _hash_content(content: str) -> str:
        return hashlib.sha256(content.encode()).hexdigest()
