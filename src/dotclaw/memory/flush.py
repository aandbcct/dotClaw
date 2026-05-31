"""MemoryFlushManager — L2 日记忆写入（含同日去重）"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..memory.store import SessionMessage

logger = logging.getLogger("dotclaw.memory.flush")


class MemoryFlushManager:
    """将对话摘要写入日记忆文件"""

    def __init__(self, workspace_dir: Path, llm: "LLMProxy | None" = None):
        self._workspace = workspace_dir
        self._memory_dir = workspace_dir / "memory"
        self._memory_dir.mkdir(parents=True, exist_ok=True)
        self._llm = llm

    async def flush_from_messages(
        self,
        messages: list,
        reason: str = "threshold",
        max_messages: int = 10,
    ) -> bool:
        """
        1. 取最近 N 条消息
        2. 生成简单摘要
        3. 同日去重
        4. 追加到 memory/YYYY-MM-DD.md
        """
        if not messages:
            return False

        # 取最近的消息
        recent = messages[-max_messages:]
        content = "\n".join(
            f"[{m.role}] {m.content[:200]}" for m in recent
        )

        # 生成简单摘要
        summary = self._generate_summary(recent)

        # 同日去重
        today = datetime.now().strftime("%Y-%m-%d")
        path = self._memory_dir / f"{today}.md"
        content_hash = hashlib.sha256(summary.encode()).hexdigest()[:16]

        if path.exists():
            existing = path.read_text(encoding="utf-8")
            if content_hash in existing:
                logger.debug(f"同日去重：{today} 已有相同摘要")
                return False

        # 追加写入
        timestamp = datetime.now().strftime("%H:%M")
        entry = f"\n## {timestamp}\n{summary}\n"
        with open(path, "a", encoding="utf-8") as f:
            f.write(entry)

        logger.info(f"日记忆已写入: {path} ({reason})")
        return True

    def _generate_summary(self, messages: list) -> str:
        """从消息列表生成简单摘要"""
        if not messages:
            return "（空对话）"

        user_parts = []
        assistant_parts = []
        for m in messages:
            content = m.content[:100] if hasattr(m, 'content') else str(m)[:100]
            role = getattr(m, 'role', 'unknown')
            if role == 'user':
                user_parts.append(content)
            elif role == 'assistant':
                assistant_parts.append(content)

        user_text = " ".join(user_parts)
        assistant_text = " ".join(assistant_parts)

        return f"- 用户讨论了: {user_text[:200]}\n- 助手回复了: {assistant_text[:200]}"

    @staticmethod
    def _hash_content(content: str) -> str:
        return hashlib.sha256(content.encode()).hexdigest()
