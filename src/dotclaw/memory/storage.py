"""MemoryStorage — SQLite + FTS5 双索引 + embedding BLOB 存储 + 文件变更检测"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

try:
    import numpy as np
    _NUMPY_AVAILABLE = True
except ImportError:
    np = None  # type: ignore
    _NUMPY_AVAILABLE = False

logger = logging.getLogger("dotclaw.memory.storage")


@dataclass
class MemoryChunk:
    id: str
    path: str
    start_line: int
    end_line: int
    text: str
    embedding: list[float] | None
    hash: str
    source: str  # "memory" | "session" | "knowledge"
    metadata: dict | None = None


@dataclass
class SearchResult:
    path: str
    start_line: int
    end_line: int
    score: float
    snippet: str
    source: str


class MemoryStorage:
    """SQLite + FTS5 双索引 + 向量检索"""

    def __init__(self, db_path: Path):
        self._db_path = db_path
        self._conn: sqlite3.Connection | None = None
        self._connect()

    def _connect(self):
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._create_tables()

    def _create_tables(self):
        assert self._conn is not None
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS chunks (
                id TEXT PRIMARY KEY,
                path TEXT NOT NULL,
                start_line INTEGER NOT NULL,
                end_line INTEGER NOT NULL,
                text TEXT NOT NULL,
                embedding BLOB,
                hash TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'memory',
                metadata TEXT,
                created_at INTEGER DEFAULT (strftime('%s', 'now')),
                updated_at INTEGER DEFAULT (strftime('%s', 'now'))
            );

            CREATE TABLE IF NOT EXISTS files (
                path TEXT PRIMARY KEY,
                hash TEXT NOT NULL,
                mtime INTEGER NOT NULL,
                size INTEGER NOT NULL,
                updated_at INTEGER DEFAULT (strftime('%s', 'now'))
            );

            CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
                text, id UNINDEXED, path UNINDEXED, source UNINDEXED,
                content='chunks', content_rowid='rowid'
            );

            CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts_trigram USING fts5(
                text, id UNINDEXED, path UNINDEXED, source UNINDEXED,
                content='chunks', content_rowid='rowid',
                tokenize='trigram case_sensitive 0'
            );
        """)
        self._conn.commit()

    def _rebuild_fts(self):
        """重建双 FTS5 索引（各自 try/except 独立，避免一个失败影响另一个）"""
        assert self._conn is not None
        try:
            self._conn.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('rebuild')")
        except Exception as e:
            logger.warning(f"FTS5 unicode61 重建失败: {e}")
        try:
            self._conn.execute("INSERT INTO chunks_fts_trigram(chunks_fts_trigram) VALUES('rebuild')")
        except Exception as e:
            logger.warning(f"FTS5 trigram 重建失败: {e}")

    # ---- 写入 ----

    def save_chunks_batch(self, chunks: Sequence[MemoryChunk]):
        assert self._conn is not None
        for c in chunks:
            emb_blob = None
            if c.embedding:
                if _NUMPY_AVAILABLE:
                    emb_blob = np.array(c.embedding, dtype=np.float32).tobytes()
                else:
                    import json
                    emb_blob = json.dumps(c.embedding).encode()

            self._conn.execute(
                """INSERT INTO chunks (id, path, start_line, end_line, text, embedding, hash, source, metadata)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                       text=excluded.text, embedding=excluded.embedding,
                       hash=excluded.hash, updated_at=strftime('%s', 'now')""",
                (c.id, c.path, c.start_line, c.end_line, c.text,
                 emb_blob, c.hash, c.source, __import__('json').dumps(c.metadata) if c.metadata else None),
            )
        self._conn.commit()

        # 重建 FTS5（UPSERT 不自动更新 FTS5）
        try:
            self._rebuild_fts()
        except Exception as e:
            logger.warning(f"FTS5 rebuild failed: {e}")

    def delete_by_path(self, path: str):
        assert self._conn is not None
        self._conn.execute("DELETE FROM chunks WHERE path = ?", (path,))
        self._conn.execute("DELETE FROM files WHERE path = ?", (path,))
        self._conn.commit()

    # ---- 文件变更检测 ----

    def get_file_state(self, path: str) -> tuple[str, int, int] | None:
        """返回 (hash, mtime, size) 或 None"""
        assert self._conn is not None
        row = self._conn.execute(
            "SELECT hash, mtime, size FROM files WHERE path = ?", (path,)
        ).fetchone()
        return row if row else None

    def upsert_file_state(self, path: str, hash_val: str, mtime: int, size: int):
        assert self._conn is not None
        self._conn.execute(
            """INSERT INTO files (path, hash, mtime, size, updated_at)
               VALUES (?, ?, ?, ?, strftime('%s', 'now'))
               ON CONFLICT(path) DO UPDATE SET
                   hash=excluded.hash, mtime=excluded.mtime,
                   size=excluded.size, updated_at=strftime('%s', 'now')""",
            (path, hash_val, mtime, size),
        )
        self._conn.commit()

    # ---- 关键词搜索 ----

    def search_keyword(self, query: str, limit: int = 10) -> list[SearchResult]:
        """FTS5 + trigram 关键词搜索"""
        assert self._conn is not None
        results = []

        # 检测 CJK（含扩展区、日文假名）
        _CJK_RE = __import__('re').compile(
            r'[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff\u3040-\u309f\u30a0-\u30ff]'
        )
        cjk = bool(_CJK_RE.search(query))

        if cjk and len(query.replace(" ", "")) >= 3:
            # 中文 trigram（≥3 字符，避免 FTS5 trigram 短查询异常）
            try:
                rows = self._conn.execute(
                    """SELECT c.path, c.start_line, c.end_line, c.text, c.source,
                              chunks_fts_trigram.rank AS score
                       FROM chunks_fts_trigram
                       JOIN chunks c ON c.rowid = chunks_fts_trigram.rowid
                       WHERE chunks_fts_trigram MATCH ?
                       ORDER BY score LIMIT ?""",
                    (query, limit),
                ).fetchall()
            except Exception:
                rows = []
        else:
            # 英文：unicode61
            try:
                rows = self._conn.execute(
                    """SELECT c.path, c.start_line, c.end_line, c.text, c.source,
                              chunks_fts.rank AS score
                       FROM chunks_fts
                       JOIN chunks c ON c.rowid = chunks_fts.rowid
                       WHERE chunks_fts MATCH ?
                       ORDER BY score LIMIT ?""",
                    (query, limit),
                ).fetchall()
            except Exception:
                rows = []

        for path, sl, el, text, source, score in rows:
            results.append(SearchResult(
                path=path, start_line=sl, end_line=el,
                score=float(score) if score else 0,
                snippet=text[:200],
                source=source,
            ))

        # FTS5 无结果时降级 LIKE
        if not results:
            like_query = f"%{query}%"
            rows = self._conn.execute(
                "SELECT path, start_line, end_line, text, source FROM chunks "
                "WHERE text LIKE ? LIMIT ?",
                (like_query, limit),
            ).fetchall()
            for path, sl, el, text, source in rows:
                results.append(SearchResult(
                    path=path, start_line=sl, end_line=el,
                    score=0.05, snippet=text[:200], source=source,
                ))

        return results

    # ---- 向量检索 ----

    def search_vector(
        self, query_embedding: list[float], limit: int = 10
    ) -> list[SearchResult]:
        """向量余弦相似度检索（numpy 向量化，无 numpy 时纯 Python 降级）"""
        assert self._conn is not None
        rows = self._conn.execute(
            "SELECT path, start_line, end_line, text, source, embedding "
            "FROM chunks WHERE embedding IS NOT NULL"
        ).fetchall()

        if not rows:
            return []

        if _NUMPY_AVAILABLE:
            return self._search_vector_numpy(query_embedding, rows, limit)
        else:
            logger.warning("numpy 未安装，降级为纯 Python 向量检索（性能差 ~100x）")
            return self._search_vector_python(query_embedding, rows, limit)

    def _search_vector_numpy(self, query_emb, rows, limit) -> list[SearchResult]:
        query_vec = np.array(query_emb, dtype=np.float32)
        results = []
        for path, sl, el, text, source, emb_blob in rows:
            chunk_vec = np.frombuffer(emb_blob, dtype=np.float32)
            dot = float(np.dot(query_vec, chunk_vec))
            norm_q = float(np.linalg.norm(query_vec))
            norm_c = float(np.linalg.norm(chunk_vec))
            score = dot / (norm_q * norm_c) if norm_q > 0 and norm_c > 0 else 0.0
            results.append(SearchResult(
                path=path, start_line=sl, end_line=el,
                score=score, snippet=text[:200], source=source,
            ))
        results.sort(key=lambda r: r.score, reverse=True)
        return results[:limit]

    def _search_vector_python(self, query_emb, rows, limit) -> list[SearchResult]:
        import json
        import math
        results = []
        for path, sl, el, text, source, emb_blob in rows:
            try:
                chunk_vec = json.loads(emb_blob.decode()) if isinstance(emb_blob, bytes) else emb_blob
            except Exception:
                continue
            dot = sum(a * b for a, b in zip(query_emb, chunk_vec))
            norm_q = math.sqrt(sum(a * a for a in query_emb))
            norm_c = math.sqrt(sum(b * b for b in chunk_vec))
            score = dot / (norm_q * norm_c) if norm_q > 0 and norm_c > 0 else 0.0
            results.append(SearchResult(
                path=path, start_line=sl, end_line=el,
                score=score, snippet=text[:200], source=source,
            ))
        results.sort(key=lambda r: r.score, reverse=True)
        return results[:limit]

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None
