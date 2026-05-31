"""DeepDream — L3 蒸馏：日记忆 → MEMORY.md 长期记忆"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("dotclaw.memory.dream")


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
        3. 蒸馏合并
        4. 写入 MEMORY.md
        5. 更新 .dream_state.json
        6. 返回摘要
        """
        state = self._load_state()
        daily_files = sorted(self._memory_dir.glob("????-??-??.md"))

        newly_distilled = 0
        new_entries: list[str] = []

        for f in daily_files:
            date_key = f.stem

            # 检查是否已蒸馏
            entry = state.get(date_key, {})
            if entry.get("distilled_at") and not force:
                continue

            content = f.read_text(encoding="utf-8")
            new_entries.append(f"## {date_key}\n{content}")
            newly_distilled += 1
            state[date_key] = {
                "distilled_at": datetime.now().isoformat(),
                "entries": len(content.splitlines()),
                "hash": self._hash_content(content),
            }

        if newly_distilled == 0:
            return "已蒸馏 0 日，无新记忆"

        # 读取现有 MEMORY.md
        existing = ""
        if self._memo_path.exists():
            existing = self._memo_path.read_text(encoding="utf-8")

        # 合并写入
        merged = existing
        for entry_text in new_entries:
            if entry_text not in merged:
                merged += f"\n{entry_text}\n"

        self._memo_path.write_text(merged, encoding="utf-8")
        self._save_state(state)

        return f"已蒸馏 {newly_distilled} 日记忆"

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
        import hashlib
        return hashlib.sha256(content.encode()).hexdigest()[:16]
