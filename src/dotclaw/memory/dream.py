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
    from .manager import MemoryManager

logger = logging.getLogger("dotclaw.memory.dream")

DREAM_SYSTEM_PROMPT = """你是记忆提炼助手。阅读以下对话摘要和已有长期记忆，合并提炼为结构化的 Markdown 文档。

要求：
1. 按主题分组，每组以 '## 主题名' 作为标题（如 "## 身份信息"、"## 项目决策"、"## 生活偏好" 等）
2. 每个 '##' 分组下，用 '- [日期] 内容' 格式列出具体记忆条目
3. 与已有记忆语义相近的条目合并而非新增
4. 忽略闲聊和重复信息
5. 主题名应简洁准确（2-6 个中文字），覆盖该组所有条目的共性"""


class DeepDream:
    """L3 蒸馏引擎"""

    DREAM_STATE_FILE: str = ".dream_state.json"

    def __init__(
        self,
        workspace_dir: Path,
        llm: "LLMProxy | None" = None,
        memory_manager: "MemoryManager | None" = None,
    ):
        self._workspace: Path = workspace_dir
        self._memory_dir: Path = workspace_dir / "memory"
        self._memory_dir.mkdir(parents=True, exist_ok=True)
        self._llm: "LLMProxy | None" = llm
        self._memory_mgr: "MemoryManager | None" = memory_manager
        self._memo_path: Path = self._memory_dir / "MEMORY.md"
        self._state_path: Path = self._memory_dir / self.DREAM_STATE_FILE

    async def run(self, force: bool = False) -> str:
        """
        1. 读取所有日记忆文件
        2. 读取当前 MEMORY.md
        3. 调用 LLM：合并 + 语义去重 + 提炼关键信息
        4. 写入 MEMORY.md
        5. 触发 sync 入库向量索引
        6. 更新 .dream_state.json
        7. 返回蒸馏摘要
        """
        state: dict = self._load_state()
        daily_files: list[Path] = sorted(self._memory_dir.glob("????-??-??.md"))

        # 收集未蒸馏的日记
        new_dates: list[str] = []
        new_content: list[str] = []

        for f in daily_files:
            date_key: str = f.stem
            entry: dict = state.get(date_key, {})
            if entry.get("distilled_at") and not force:
                continue
            new_dates.append(date_key)
            new_content.append(f.read_text(encoding="utf-8"))

        if not new_dates:
            return "已蒸馏 0 日，无新记忆"

        # 读取现有长期记忆
        existing_memory: str = ""
        if self._memo_path.exists():
            existing_memory = self._memo_path.read_text(encoding="utf-8")

        # 调用 LLM 蒸馏
        try:
            distilled: str = await self._distill_with_llm(existing_memory, new_content, new_dates)
        except Exception as e:
            logger.error(f"LLM 蒸馏失败: {e}")
            return f"蒸馏失败: {e}"

        # 写入前备份旧版本
        if self._memo_path.exists() and self._memo_path.stat().st_size > 0:
            backup_path: Path = self._memo_path.with_suffix(".md.bak")
            backup_path.write_text(
                self._memo_path.read_text(encoding="utf-8"), encoding="utf-8"
            )
            logger.debug(f"MEMORY.md 已备份: {backup_path}")

        # 写入 MEMORY.md
        self._memo_path.write_text(distilled, encoding="utf-8")

        # 触发 sync 入库向量索引
        if self._memory_mgr:
            try:
                await self._memory_mgr.sync(force=True)
                logger.info("MEMORY.md 已同步到向量库")
            except Exception as e:
                logger.warning(f"MEMORY.md sync 失败: {e}")

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

        date_labels: str = ", ".join(dates)
        daily_text: str = "\n\n---\n\n".join(daily_contents)

        user_content: str = f"""日期: {date_labels}

已有长期记忆:
{existing if existing else "(暂无)"}

新对话摘要:
{daily_text}

请将以上新对话摘要与已有长期记忆合并，提炼为结构化的 Markdown 文档（按主题分组，每组以 '## 主题名' 开头）。"""

        messages: list[LLMMessage] = [
            LLMMessage(role="system", content=DREAM_SYSTEM_PROMPT),
            LLMMessage(role="user", content=user_content),
        ]

        result: str = ""
        async for chunk in self._llm.chat(messages=messages, stream=False):
            result += chunk.content
        return result.strip()

    def _load_state(self) -> dict:
        if self._state_path.exists():
            return json.loads(self._state_path.read_text(encoding="utf-8"))
        return {}

    def _save_state(self, state: dict) -> None:
        self._state_path.write_text(
            json.dumps(state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    @staticmethod
    def _hash_content(content: str) -> str:
        return hashlib.sha256(content.encode()).hexdigest()[:16]
