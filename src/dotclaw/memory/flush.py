"""MemoryFlushManager — L2 日记忆写入（LLM 摘要 + 同日去重）"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from ..llm.base import Message as LLMMessage

if TYPE_CHECKING:
    from ..memory.store import SessionMessage
    from ..llm.proxy import LLMProxy

logger = logging.getLogger("dotclaw.memory.flush")

FLUSH_SYSTEM_PROMPT = """你是对话摘要助手。根据以下对话记录，生成 2-3 句中文摘要。

要求：
1. 提取用户的主要话题、提出的问题、做出的决策
2. 提取 AI 提供的关键信息、建议、结论
3. 忽略闲聊、问候等无信息量的内容
4. 纯文本输出，不要标题、编号、Markdown 格式"""


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
        1. 取最近 N 条消息（含 user + assistant 完整往返）
        2. 调用 LLM 生成 2-3 句中文摘要
        3. 检查同日已有摘要的 content hash — 相同则跳过
        4. 追加到 data/memory/YYYY-MM-DD.md
        5. 异常静默（后台 asyncio.create_task，不抛给调用方）
        """
        if not messages:
            return False

        recent = messages[-max_messages:]

        try:
            summary = await self._summarize_with_llm(recent)
        except Exception as e:
            logger.warning(f"LLM 摘要生成失败，降级为模板摘要: {e}")
            summary = self._generate_fallback_summary(recent)

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

    async def _summarize_with_llm(self, messages: list) -> str:
        """调用 LLM 生成对话摘要"""
        if not self._llm:
            raise RuntimeError("LLM 未配置")

        # 构建对话记录文本
        dialog_lines = []
        for m in messages:
            role = getattr(m, "role", "unknown")
            content = getattr(m, "content", "")[:500]
            label = {"user": "用户", "assistant": "AI"}.get(role, role)
            dialog_lines.append(f"{label}: {content}")

        dialog_text = "\n".join(dialog_lines)

        llm_messages = [
            LLMMessage(role="system", content=FLUSH_SYSTEM_PROMPT),
            LLMMessage(role="user", content=f"对话记录：\n\n{dialog_text}"),
        ]

        result = ""
        async for chunk in self._llm.chat(messages=llm_messages, stream=False):
            result += chunk.content
        return result.strip()

    def _generate_fallback_summary(self, messages: list) -> str:
        """LLM 不可用时的降级摘要"""
        if not messages:
            return "（空对话）"

        user_parts = []
        assistant_parts = []
        for m in messages:
            content = getattr(m, "content", "")[:100]
            role = getattr(m, "role", "unknown")
            if role == "user":
                user_parts.append(content)
            elif role == "assistant":
                assistant_parts.append(content)

        user_text = " ".join(user_parts)
        assistant_text = " ".join(assistant_parts)

        return f"- 用户讨论了: {user_text[:200]}\n- 助手回复了: {assistant_text[:200]}"

    @staticmethod
    def _hash_content(content: str) -> str:
        return hashlib.sha256(content.encode()).hexdigest()
