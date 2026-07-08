"""测试 AgentMessage —— Agent 间通信消息。"""

from dotclaw.orchestration.agent_message import AgentMessage, AgentMessageType


class TestAgentMessageType:
    """AgentMessageType 枚举。"""

    def test_all_types_defined(self) -> None:
        values: set[str] = {t.value for t in AgentMessageType}
        assert values == {"steer", "heartbeat"}


class TestAgentMessage:
    """AgentMessage 构造。"""

    def test_steer_message(self) -> None:
        msg = AgentMessage(
            message_id="m1",
            sender_id="parent",
            receiver_id="child",
            msg_type=AgentMessageType.STEER,
            content="补充信息：使用 Python 3.13",
            task_id="t1",
        )
        assert msg.msg_type == AgentMessageType.STEER
        assert msg.content == "补充信息：使用 Python 3.13"
        assert msg.progress == 0.0

    def test_heartbeat_message(self) -> None:
        msg = AgentMessage(
            message_id="m2",
            sender_id="child",
            receiver_id="parent",
            msg_type=AgentMessageType.HEARTBEAT,
            content="处理中...",
            progress=0.5,
        )
        assert msg.msg_type == AgentMessageType.HEARTBEAT
        assert msg.progress == 0.5

    def test_default_values(self) -> None:
        msg = AgentMessage(
            message_id="m3",
            sender_id="a",
            receiver_id="b",
            msg_type=AgentMessageType.STEER,
        )
        assert msg.content == ""
        assert msg.progress == 0.0
        assert msg.task_id == ""
        assert msg.timestamp != ""
