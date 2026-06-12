"""Journal Sinks —— 事件输出端。"""

from dotclaw.journal.sinks.console import console_sink
from dotclaw.journal.sinks.trace import trace_sink

__all__ = ["console_sink", "trace_sink"]
