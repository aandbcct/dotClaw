"""State —— 会话级持久状态。

State 与 thread_id（Session ID）绑定，生命周期跟随 Session，
不跟随单次 Task/AgentRun。
同一 Session 下的多个 Task 共享读写同一份 State。

State 由 Runtime.StateStore 管理持久化，不在 Agent 内存中。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from ..llm.base import Message


@dataclass
class State:
    """会话级持久状态 —— 与 thread_id 绑定。

    State 字段分为三组：
    1. 标识：thread_id（主键，对应 Session.id）
    2. 内容：messages（共享消息历史）、agent_outputs（各 Agent 产出）
    3. 元数据：metadata、时间戳

    设计原则：
    - State 是 Agent 生产者和消费者的共享数据
    - Agent 不持有 State，通过 Runtime 读写
    - 多 Agent 协作时通过同一 State 分区的 messages 同步信息
    """

    thread_id: str
    """Session 唯一标识，State 的主键。"""

    messages: list[Message] = field(default_factory=list)
    """共享消息历史。包含 user/assistant/tool 等角色的对话记录。
    
    不同于 Session.history（易失性 LLM 上下文），State.messages 是
    持久化的对话内容，可在多次 AgentRun 之间保持。
    """

    agent_outputs: dict[str, Any] = field(default_factory=dict)
    """各 Agent 的产出数据。key = agent_id，value = 任意结构化数据。
    
    用于多 Agent 协作时交换中间结果。
    示例：{"researcher": {"findings": [...]}, "coder": {"file": "..."}}
    """

    metadata: dict[str, Any] = field(default_factory=dict)
    """扩展元数据。可用于存储任务目标、约束条件、用户偏好等。"""

    created_at: str = ""
    """State 创建时间（ISO 8601）。"""

    updated_at: str = ""
    """State 最后更新时间（ISO 8601）。"""

    # ── 工厂方法 ──

    @classmethod
    def new(cls, thread_id: str) -> State:
        """创建新 State 实例。

        Args:
            thread_id: Session ID，用于标识和隔离状态

        Returns:
            初始化好的 State 实例，created_at/updated_at 设为当前时间
        """
        now: str = datetime.now(timezone.utc).isoformat()
        return cls(
            thread_id=thread_id,
            created_at=now,
            updated_at=now,
        )

    # ── 内容操作 ──

    def add_message(self, message: Message) -> None:
        """追加一条消息到共享历史。

        Args:
            message: 要追加的 Message 对象
        """
        self.messages.append(message)

    def set_agent_output(self, agent_id: str, output: Any) -> None:
        """写入特定 Agent 的产出数据。

        Args:
            agent_id: Agent 标识
            output: 产出数据（任意可序列化类型）
        """
        self.agent_outputs[agent_id] = output

    def get_agent_output(self, agent_id: str) -> Any | None:
        """读取特定 Agent 的产出数据。

        Args:
            agent_id: Agent 标识

        Returns:
            产出数据，不存在时返回 None
        """
        return self.agent_outputs.get(agent_id)

    def touch(self) -> None:
        """更新 updated_at 时间戳为当前时间。"""
        self.updated_at = datetime.now(timezone.utc).isoformat()
