"""测试 AgentRun —— 一次原子调用的完整记录。"""

from dotclaw.session.agent_run import AgentRun
from dotclaw.llm.base import Message


class TestAgentRun:
    """AgentRun 字段和序列化。"""

    def test_basic_fields(self) -> None:
        """基本字段正确赋值。"""
        run = AgentRun(
            run_id="run-001",
            agent_id="agent-abc",
            tool_calls=3,
            tokens_in=100,
            tokens_out=200,
            iterations=5,
            duration_ms=500,
            end_status="completed",
        )
        assert run.run_id == "run-001"
        assert run.agent_id == "agent-abc"
        assert run.tool_calls == 3
        assert run.tokens_in == 100
        assert run.tokens_out == 200
        assert run.iterations == 5
        assert run.duration_ms == 500
        assert run.end_status == "completed"
        assert run.error is None
        assert run.parent_run_id == ""

    def test_failed_status(self) -> None:
        """失败状态记 error。"""
        run = AgentRun(
            run_id="run-err",
            end_status="failed",
            error="something broke",
        )
        assert run.end_status == "failed"
        assert run.error == "something broke"

    def test_handoff_status(self) -> None:
        """handoff 状态。"""
        run = AgentRun(
            run_id="run-ho",
            end_status="handoff",
        )
        assert run.end_status == "handoff"

    def test_final_output_from_messages(self) -> None:
        """从 messages 提取最后一条无 tool_calls 的 assistant 消息。"""
        msgs = [
            Message(role="assistant", content="step1", tool_calls=[1]),
            Message(role="tool", content="result1"),
            Message(role="assistant", content="final answer"),
        ]
        run = AgentRun(run_id="run-1", messages=msgs)
        assert run.final_output == "final answer"

    def test_final_output_none_when_no_text_response(self) -> None:
        """所有 assistant 都有 tool_calls 时返回 None。"""
        msgs = [
            Message(role="assistant", content="step1", tool_calls=[1]),
            Message(role="tool", content="result1"),
        ]
        run = AgentRun(run_id="run-1", messages=msgs)
        assert run.final_output is None

    def test_serialize_deserialize(self) -> None:
        """to_dict / from_dict 往返一致。"""
        msgs = [
            Message(role="assistant", content="hello", tool_calls=[1, 2]),
            Message(role="tool", content="result", tool_call_id="tc-1"),
        ]
        run = AgentRun(
            run_id="run-s",
            agent_id="a1",
            parent_run_id="p1",
            messages=msgs,
            end_status="completed",
            tool_calls=2,
            tokens_in=50,
            tokens_out=100,
            iterations=3,
            duration_ms=300,
        )
        data = run.to_dict()
        restored = AgentRun.from_dict(data)
        assert restored.run_id == run.run_id
        assert restored.agent_id == run.agent_id
        assert restored.parent_run_id == run.parent_run_id
        assert restored.end_status == run.end_status
        assert restored.tool_calls == run.tool_calls
        assert len(restored.messages) == 2
        assert restored.messages[0].role == "assistant"
        assert restored.messages[0].tool_calls == [1, 2]
        assert restored.messages[1].tool_call_id == "tc-1"
