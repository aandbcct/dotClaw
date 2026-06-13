"""trace_sink —— 实时追加事件到 trace.jsonl。"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from dotclaw.journal.events import AgentEvent

logger = logging.getLogger("dotclaw.journal.sinks.trace")


def _ensure_dir(path: Path) -> None:
    """确保目录存在。"""
    path.mkdir(parents=True, exist_ok=True)


def trace_sink(
    event: AgentEvent,
    output_dir: str | Path,
    request_id: str,
    session_start_ts: float = 0.0,
    date_str: str | None = None,
) -> None:
    """实时追加事件到 trace.jsonl。

    Args:
        event: Journal 事件。
        output_dir: 输出根目录（如 "./data/traces"）。
        request_id: 请求 ID。
        session_start_ts: 会话开始时间戳，用于子目录前缀。
        date_str: 日期字符串（YYYY-MM-DD），默认用今天。
    """
    import datetime

    if date_str is None:
        date_str = datetime.date.today().isoformat()

    ts_str = f"{int(session_start_ts)}" if session_start_ts else str(int(event.timestamp))
    trace_dir = Path(output_dir) / date_str / f"{ts_str}_{request_id}"
    _ensure_dir(trace_dir)

    filepath = trace_dir / "trace.jsonl"

    line = json.dumps({
        "ts": event.timestamp,
        "t": event.created_at,
        "type": event.event_type,
        "data": event.data,
    }, ensure_ascii=False)

    try:
        with open(filepath, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError as e:
        logger.error(f"Failed to write trace line: {e}")
