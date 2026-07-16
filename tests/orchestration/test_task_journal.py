"""测试 Task Journal 生命周期事件的隐私边界。"""

from __future__ import annotations

from dotclaw.journal import Journal
from dotclaw.journal.events import EventType, TaskEventType


def test_task_event_records_control_metadata_without_message_payload() -> None:
    """Task 事件只记录控制面字段，不能记录通信正文。"""
    journal: Journal = Journal()
    journal.task_event(
        event_type=TaskEventType.MESSAGE_SENT.value,
        task_id="task-1",
        endpoint="source",
        status="running_target",
        sequence=2,
    )
    event = journal._events[-1]
    assert event.event_type == EventType.TASK_LIFECYCLE
    assert event.data == {
        "action": "message_sent",
        "task_id": "task-1",
        "endpoint": "source",
        "status": "running_target",
        "sequence": 2,
    }
    assert "payload" not in event.data
    assert "content" not in event.data
