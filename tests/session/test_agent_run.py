"""测试 AgentRun —— 一次原子调用的完整记录（v2 模型）。"""

from dotclaw.session.agent_run import AgentRun, RunEndStatus, TriggerType


class TestAgentRun:
    """AgentRun 字段和序列化。"""

    def test_basic_fields(self) -> None:
        """基本字段正确赋值。"""
        run = AgentRun(
            run_id="run-001",
            agent_id="agent-abc",
            tool_calls=1,
            tokens_in=100,
            tokens_out=200,
            duration_ms=500,
            end_status=RunEndStatus.COMPLETED.value,
            trigger=TriggerType.USER_INPUT.value,
            sequence=0,
            trace_ids=["trace.message:14:05:30.123"],
        )
        assert run.run_id == "run-001"
        assert run.agent_id == "agent-abc"
        assert run.tool_calls == 1
        assert run.tokens_in == 100
        assert run.tokens_out == 200
        assert run.duration_ms == 500
        assert run.end_status == RunEndStatus.COMPLETED.value
        assert run.trigger == TriggerType.USER_INPUT.value
        assert run.sequence == 0
        assert run.error is None
        assert run.parent_run_id == ""

    def test_failed_status(self) -> None:
        """失败状态记 error。"""
        run = AgentRun(
            run_id="run-err",
            end_status=RunEndStatus.FAILED.value,
            error="something broke",
        )
        assert run.end_status == RunEndStatus.FAILED.value
        assert run.error == "something broke"

    def test_handoff_status(self) -> None:
        """handoff 状态。"""
        run = AgentRun(
            run_id="run-ho",
            end_status=RunEndStatus.HANDOFF.value,
        )
        assert run.end_status == RunEndStatus.HANDOFF.value

    def test_interrupted_status(self) -> None:
        """被中断状态。"""
        run = AgentRun(
            run_id="run-int",
            end_status=RunEndStatus.INTERRUPTED.value,
            error="user interrupted",
        )
        assert run.end_status == RunEndStatus.INTERRUPTED.value

    def test_tool_wait_status(self) -> None:
        """工具等待状态。"""
        run = AgentRun(
            run_id="run-tw",
            end_status=RunEndStatus.TOOL_WAIT.value,
        )
        assert run.end_status == RunEndStatus.TOOL_WAIT.value

    def test_final_output_from_messages(self) -> None:
        """从 messages 提取最后一条无 tool_calls 的 assistant 消息。"""
        from dotclaw.llm.base import Message, ToolCall
        tc: ToolCall = ToolCall(id="tc-1", name="search", arguments="{}")
        msgs = [
            Message(role="assistant", content="step1", tool_calls=[tc]),
            Message(role="tool", content="result1"),
            Message(role="assistant", content="final answer"),
        ]
        run = AgentRun(run_id="run-1", messages=msgs)
        assert run.final_output == "final answer"

    def test_final_output_none_when_no_text_response(self) -> None:
        """所有 assistant 都有 tool_calls 时返回 None。"""
        from dotclaw.llm.base import Message, ToolCall
        tc: ToolCall = ToolCall(id="tc-1", name="search", arguments="{}")
        msgs = [
            Message(role="assistant", content="step1", tool_calls=[tc]),
            Message(role="tool", content="result1"),
        ]
        run = AgentRun(run_id="run-1", messages=msgs)
        assert run.final_output is None

    def test_state_snapshot_fields(self) -> None:
        """state_snapshot 正确存储。"""
        snapshot: dict = {
            "task_id": "abc123",
            "phase": "done",
            "iteration": 3,
            "end_status": "completed",
            "final_output": "hello",
        }
        run = AgentRun(
            run_id="run-s",
            agent_id="a1",
            state_snapshot=snapshot,
            trace_ids=["trace.message:14:05:30.123", "llm.call_start:14:05:29.000"],
            trigger=TriggerType.TOOL_RESULT.value,
        )
        assert run.state_snapshot == snapshot
        assert run.trace_ids == ["trace.message:14:05:30.123", "llm.call_start:14:05:29.000"]
        assert run.trigger == TriggerType.TOOL_RESULT.value

    def test_serialize_deserialize(self) -> None:
        """to_dict / from_dict 往返一致。"""
        from dotclaw.llm.base import Message, ToolCall
        tc1: ToolCall = ToolCall(id="tc-1", name="search", arguments='{"q":"x"}')
        tc2: ToolCall = ToolCall(id="tc-2", name="read", arguments="{}")
        msgs = [
            Message(role="assistant", content="hello", tool_calls=[tc1, tc2]),
            Message(role="tool", content="result", tool_call_id="tc-1"),
        ]
        snapshot: dict = {
            "task_id": "abc123",
            "phase": "done",
            "iteration": 3,
            "end_status": "completed",
            "final_output": "hello",
        }
        trace_ids: list[str] = ["trace.message:14:05:30.123"]
        run = AgentRun(
            run_id="run-s",
            agent_id="a1",
            parent_run_id="p1",
            state_snapshot=snapshot,
            trace_ids=trace_ids,
            end_status=RunEndStatus.COMPLETED.value,
            trigger=TriggerType.USER_INPUT.value,
            sequence=1,
            tool_calls=2,
            tokens_in=50,
            tokens_out=100,
            duration_ms=300,
            error=None,
            started_at="2026-01-01T00:00:00Z",
            ended_at="2026-01-01T00:00:01Z",
            messages=msgs,
        )
        data = run.to_dict()
        restored = AgentRun.from_dict(data)
        assert restored.run_id == run.run_id
        assert restored.agent_id == run.agent_id
        assert restored.parent_run_id == run.parent_run_id
        assert restored.end_status == run.end_status
        assert restored.tool_calls == run.tool_calls
        assert restored.state_snapshot == snapshot
        assert restored.trace_ids == trace_ids
        assert restored.trigger == TriggerType.USER_INPUT.value
        assert restored.sequence == 1
        assert len(restored.messages) == 2
        assert restored.messages[0].role == "assistant"
        assert restored.messages[0].tool_calls is not None
        assert restored.messages[0].tool_calls[0].name == "search"
        assert restored.messages[1].tool_call_id == "tc-1"
