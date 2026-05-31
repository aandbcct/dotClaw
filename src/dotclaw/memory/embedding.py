"""EmbeddingProvider — 向量嵌入抽象 + LRU 缓存"""

from __future__ import annotations

import hashlib
import logging
from abc import ABC, abstractmethod
from collections import OrderedDict

from openai import OpenAI

logger = logging.getLogger("dotclaw.memory.embedding")


class EmbeddingProvider(ABC):
    """向量嵌入抽象基类"""

    @abstractmethod
    def embed_query(self, text: str) -> list[float]:
        """单条文本嵌入"""
        ...

    @abstractmethod
    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """批量嵌入"""
        ...


class OpenAIEmbeddingProvider(EmbeddingProvider):
    """OpenAI-compatible embedding API"""

    def __init__(
        self,
        api_base: str,
        api_key: str,
        model: str = "text-embedding-v3",
        dimensions: int = 1024,
    ):
        self._client = OpenAI(api_key=api_key, base_url=api_base)
        self._model = model
        self._dimensions = dimensions
        self._batch_size = 16  # max 16 per API call

    def embed_query(self, text: str) -> list[float]:
        return self.embed_batch([text])[0]

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        results: list[list[float]] = []
        for i in range(0, len(texts), self._batch_size):
            batch = texts[i:i + self._batch_size]
            resp = self._client.embeddings.create(
                model=self._model,
                input=batch,
                dimensions=self._dimensions,
            )
            results.extend([d.embedding for d in resp.data])
        return results


class EmbeddingCache:
    """会话级 LRU 嵌入缓存（OrderedDict，max 256 条）"""

    def __init__(self, max_size: int = 256):
        self._max_size = max_size
        self._cache: OrderedDict[str, list[float]] = OrderedDict()

    def _key(self, text: str) -> str:
        return hashlib.sha256(text.encode()).hexdigest()[:16]

    def get(self, text: str) -> list[float] | None:
        return self._cache.get(self._key(text))

    def set(self, text: str, embedding: list[float]):
        key = self._key(text)
        if key in self._cache:
            self._cache.move_to_end(key)
        else:
            if len(self._cache) >= self._max_size:
                self._cache.popitem(last=False)
            self._cache[key] = embedding

    def clear(self):
        self._cache.clear()
