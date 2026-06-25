"""state_sink —— 原子覆盖写入 state.json。"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger("dotclaw.journal.sinks.state")


class StateSink:
    """原子覆盖写入 state.json。

    使用临时文件 + 替换策略保证写入原子性。
    """

    def __init__(self, file_path: Path) -> None:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        self._path = file_path

    def write(self, state: dict) -> None:
        """覆盖写入状态文件。"""
        try:
            tmp = self._path.with_suffix(".tmp")
            payload = json.dumps(state, ensure_ascii=False, indent=2, default=str)
            tmp.write_text(payload, encoding="utf-8")
            tmp.replace(self._path)
        except OSError as e:
            logger.error(f"Failed to write state file: {e}")
