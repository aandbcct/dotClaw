"""history_sink —— 追加消息内容到 history.jsonl。"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger("dotclaw.journal.sinks.history")


class HistorySink:
    """追加写入 history.jsonl。

    逐行写入 JSON 格式的消息记录，每行一条。
    """

    def __init__(self, file_path: Path) -> None:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        self._file = open(file_path, "a", encoding="utf-8")

    def write(self, entry: dict) -> None:
        """追加一行 JSON。"""
        try:
            line = json.dumps(entry, ensure_ascii=False, default=str)
            self._file.write(line + "\n")
            self._file.flush()
        except OSError as e:
            logger.error(f"Failed to write history line: {e}")

    def close(self) -> None:
        """关闭文件句柄。"""
        try:
            self._file.close()
        except OSError:
            pass
