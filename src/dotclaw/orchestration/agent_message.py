"""AgentMessage —— Agent 间通信消息。

对标 A2A Message（Part 模型）。用于 Parent↔Child 间的运行时交互，
不经过 Task 生命周期。轻量，仅携带消息类型 + 内容。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


# ============================================================================
# AgentMessageType
# ============================================================================


class AgentMessageType(Enum):
    """Agent 间消息类型。"""

    STEER = "steer"
    """父→子：运行时干预（补充信息、修改方向）"""

    HEARTBEAT = "heartbeat"
    """子→父：定期进度汇报"""


# ============================================================================
# AgentMessage
# ============================================================================


@dataclass
class AgentMessage:
    """Agent 间通信消息。

    比 Task 更轻量：不携带完整的输入/输出侧字段，
    仅用于运行时交互（STEER/HEARTBEAT）。
    """

    message_id: str
    """消息唯一标识"""

    sender_id: str
    """发送方 agent_id"""

    receiver_id: str
    """接收方 agent_id"""

    msg_type: AgentMessageType
    """消息类型"""

    content: str = ""
    """文本内容"""

    progress: float = 0.0
    """进度（0.0-1.0）。HEARTBEAT 专用，STEER 时为 0.0。"""

    task_id: str = ""
    """关联的 Task ID。"""

    timestamp: str = ""
    """发送时间（ISO 8601）"""

    def __post_init__(self) -> None:
        if not self.timestamp:
            self.timestamp = datetime.now(timezone.utc).isoformat()
