"""Journal —— 统一观测模块。

一个入口、多路输出：事件一次发射，按需路由到控制台、trace.jsonl、report.json、snapshot.json。
"""

from dotclaw.journal.events import AgentEvent, EventType
from dotclaw.journal.journal import Journal
from dotclaw.journal.storage import diff_snapshots, load_snapshot, save_snapshot

__all__ = [
    "Journal",
    "AgentEvent",
    "EventType",
    "save_snapshot",
    "load_snapshot",
    "diff_snapshots",
]
