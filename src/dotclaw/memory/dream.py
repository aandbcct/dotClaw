"""DeepDream — L3 蒸馏：日记忆 → MEMORY.md 长期记忆（LLM 语义合并 + 去重）"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from ..llm.base import Message as LLMMessage

if TYPE_CHECKING:
    from ..llm.proxy import LLMProxy

logger = logging.getLogger("dotclaw.memory.dream")

DREAM_SYSTEM_PROMPT = """你是记忆提炼助手。阅读以下对话摘要和已有长期记忆，合并提炼为简洁的 Markdown 列表。

要求：
1. 提取用户偏好、重要决策、待办事项、学到的知识
2. 与已有记忆语义相近的条目合并而非新增
3. 忽略闲聊和重复信息
4. 每行格式：'- [日期] 内容'"""


class DeepDream:
    """L3 蒸馏引擎"""

    DREAM_STATE_FILE = ".dream_state.json"

    def __init__(self, workspace_dir: Path, llm: "LLMProxy | None" = None):
        self._workspace = workspace_dir
        self._memory_dir = workspace_dir / "memory"
        self._memory_dir.mkdir(parents=True, exist_ok=True)
        self._llm = llm
        self._memo_path = self._memory_dir / "MEMORY.md"
        self._state_path = self._memory_dir / self.DREAM_STATE_FILE

    async def run(self, force: bool = False) -> str:
        """
        1. 读取所有日记忆文件
        2. 读取当前 MEMORY.md
        3. 调用 LLM：合并 + 语义去重 + 提炼关键信息
        4. 写入 MEMORY.md
        5. 更新 .dream_state.json
        6. 返回蒸馏摘要
        """
        state = self._load_state()
        daily_files = sorted(self._memory_dir.glob("????-??-??.md"))

        # 收集未蒸馏的日记
        new_dates: list[str] = []
        new_content: list[str] = []

        for f in daily_files:
            date_key = f.stem
            entry = state.get(date_key, {})
            if entry.get("distilled_at") and not force:
                continue
            new_dates.append(date_key)
            new_content.append(f.read_text(encoding="utf-8"))

        if not new_dates:
            return "已蒸馏 0 日，无新记忆"

        # 读取现有长期记忆
        existing_memory = ""
        if self._memo_path.exists():
            existing_memory = self._memo_path.read_text(encoding="utf-8")

        # 调用 LLM 蒸馏
        try:
            distilled = await self._distill_with_llm(existing_memory, new_content, new_dates)
        except Exception as e:
            logger.error(f"LLM 蒸馏失败: {e}")
            return f"蒸馏失败: {e}"

        # 写入前备份旧版本
        if self._memo_path.exists() and self._memo_path.stat().st_size > 0:
            backup_path = self._memo_path.with_suffix(".md.bak")
            backup_path.write_text(
                self._memo_path.read_text(encoding="utf-8"), encoding="utf-8"
            )
            logger.debug(f"MEMORY.md 已备份: {backup_path}")

        # 写入 MEMORY.md
        self._memo_path.write_text(distilled, encoding="utf-8")

        # 更新状态
        for d in new_dates:
            state[d] = {
                "distilled_at": datetime.now().isoformat(),
                "entries": len(distilled.splitlines()),
                "hash": self._hash_content(distilled[:200]),
            }
        self._save_state(state)

        return f"已蒸馏 {len(new_dates)} 日记忆"

    async def _distill_with_llm(
        self,
        existing: str,
        daily_contents: list[str],
        dates: list[str],
    ) -> str:
        """调用 LLM 做语义合并蒸馏"""
        if not self._llm:
            return existing + "\n\n" + "\n\n".join(daily_contents)

        date_labels = ", ".join(dates)
        daily_text = "\n\n---\n\n".join(daily_contents)

        user_content = f"""日期: {date_labels}

已有长期记忆:
{existing if existing else "(暂无)"}

新对话摘要:
{daily_text}

请将以上新对话摘要与已有长期记忆合并，提炼为简洁的 Markdown 列表。"""

        messages = [
            LLMMessage(role="system", content=DREAM_SYSTEM_PROMPT),
            LLMMessage(role="user", content=user_content),
        ]

        result = ""
        async for chunk in self._llm.chat(messages=messages, stream=False):
            result += chunk.content
        return result.strip()

    def _load_state(self) -> dict:
        if self._state_path.exists():
            return json.loads(self._state_path.read_text(encoding="utf-8"))
        return {}

    def _save_state(self, state: dict):
        self._state_path.write_text(
            json.dumps(state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    @staticmethod
    def _hash_content(content: str) -> str:
        return hashlib.sha256(content.encode()).hexdigest()[:16]
