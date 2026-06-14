"""console_sink —— 实时输出 ERROR/WARNING 到控制台。"""

import sys

from dotclaw.journal.events import AgentEvent, EventType

_ERROR_LEVELS = {"ERROR", "WARNING"}

# todo 使用rich库设计一套终端监控看板，好看
def console_sink(event: AgentEvent) -> None:
    """实时输出 ERROR 和 WARNING 事件到 stderr。

    Args:
        event: ERROR 或 WARNING 事件。
    """
    if event.event_type != EventType.ERROR:
        return

    data = event.data
    level = data.get("level", "ERROR")
    if level not in _ERROR_LEVELS:
        return

    source = data.get("source", "unknown")
    message = data.get("message", "")
    print(f"[{level}] {source}: {message}", file=sys.stderr)
