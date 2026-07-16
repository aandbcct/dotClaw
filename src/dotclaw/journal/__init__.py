"""Journal —— 统一观测模块。

一个入口、多路输出：事件一次发射，按需路由到 trace.jsonl、report.json、snapshot.json。
v2：history 合并入 trace，所有消息以 TRACE_MESSAGE 事件写入 trace.jsonl。
"""

from dotclaw.journal.events import AgentEvent, EventType, TraceMessageRole
from dotclaw.journal.journal import Journal
from dotclaw.journal.storage import diff_snapshots, load_snapshot, save_snapshot

__all__ = [
    "Journal",
    "AgentEvent",
    "EventType",
    "TraceMessageRole",
    "save_snapshot",
    "load_snapshot",
    "diff_snapshots",
]
