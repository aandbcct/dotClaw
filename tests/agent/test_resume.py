"""Agent Resume 功能测试（v2 StateStore + trace.jsonl 架构）。

测试 ResumeManager 的 get_resume_context() 从 StateStore + trace.jsonl 恢复。
"""

import json
from pathlib import Path

import pytest

from dotclaw.llm.base import Message, ToolCall


# ═══════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════

def _write_state(trace_dir: Path, session_id: str, state_data: dict) -> None:
    """在 StateStore 路径写入 state.json。"""
    state_dir = trace_dir / "session" / session_id
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "state.json").write_text(
        json.dumps(state_data, ensure_ascii=False, indent=2), encoding="utf-8")

def _write_trace_jsonl(trace_dir: Path, session_id: str,
                       entries: list[dict]) -> Path:
    """写入 trace.jsonl 条目。"""
    trace_path = trace_dir / "session" / session_id / "trace.jsonl"
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(e, ensure_ascii=False) for e in entries]
    trace_path.write_text("\n".join(lines), encoding="utf-8")
    return trace_path


# ═══════════════════════════════════════════════════════════════════
# get_resume_context
# ═══════════════════════════════════════════════════════════════════

class TestResumeContext:
    """测试 get_resume_context() 从 StateStore + trace.jsonl 恢复。"""

    @pytest.mark.asyncio
    async def test_no_resume_when_no_state(self, tmp_path):
        from dotclaw.runtime.state_store import StateStore
        from dotclaw.agent.resume import ResumeManager

        store = StateStore(data_dir=str(tmp_path))
        mgr = ResumeManager(state_store=store, trace_dir=str(tmp_path))

        ctx = await mgr.get_resume_context("s-test")
        assert ctx is None

    @pytest.mark.asyncio
    async def test_no_resume_when_state_completed(self, tmp_path):
        from dotclaw.runtime.state_store import StateStore
        from dotclaw.agent.resume import ResumeManager

        _write_state(tmp_path, "s-test", {
            "task_id": "t1", "thread_id": "s-test", "agent_id": "a1",
            "phase": "done", "iteration": 3, "max_iterations": 10,
            "end_status": "completed", "tool_calls_total": 2,
        })

        store = StateStore(data_dir=str(tmp_path))
        mgr = ResumeManager(state_store=store, trace_dir=str(tmp_path))

        ctx = await mgr.get_resume_context("s-test")
        assert ctx is None

    @pytest.mark.asyncio
    async def test_resume_when_state_tool_wait(self, tmp_path):
        from dotclaw.runtime.state_store import StateStore
        from dotclaw.agent.resume import ResumeManager

        _write_state(tmp_path, "s-test", {
            "task_id": "t1", "thread_id": "s-test", "agent_id": "a1",
            "phase": "acting", "iteration": 2, "max_iterations": 10,
            "end_status": "tool_wait", "tool_calls_total": 2,
        })

        # 写入 trace.jsonl 消息
        _write_trace_jsonl(tmp_path, "s-test", [
            {
                "ts": 1.0, "t": "00:00:01.000", "type": "trace.message",
                "agentrun_id": "run-001",
                "data": {"role": "user", "content": "query"},
            },
            {
                "ts": 2.0, "t": "00:00:02.000", "type": "trace.message",
                "agentrun_id": "run-001",
                "data": {
                    "role": "assistant", "content": "thinking",
                    "tool_calls": [
                        {"id": "c1", "name": "search", "arguments": '{"q":"x"}'},
                    ],
                },
            },
        ])

        store = StateStore(data_dir=str(tmp_path))
        mgr = ResumeManager(state_store=store, trace_dir=str(tmp_path))

        ctx = await mgr.get_resume_context("s-test")
        assert ctx is not None
        assert len(ctx["messages"]) == 2
        assert ctx["messages"][0].role == "user"
        assert ctx["messages"][1].role == "assistant"
        assert len(ctx["incomplete_tools"]) == 1
        assert ctx["incomplete_tools"][0].id == "c1"
        assert "state" in ctx
        assert ctx["state"]["end_status"] == "tool_wait"


class TestReconstructMessages:
    """测试 _reconstruct_messages() 消息重建。"""

    def test_reconstruct_complete_conversation(self, tmp_path):
        from dotclaw.agent.resume import ResumeManager

        entries: list[dict] = [
            {
                "ts": 1.0, "t": "00:00:01.000", "type": "trace.message",
                "agentrun_id": "run-001",
                "data": {"role": "user", "content": "hello"},
            },
            {
                "ts": 2.0, "t": "00:00:02.000", "type": "trace.message",
                "agentrun_id": "run-001",
                "data": {"role": "assistant", "content": "hi there"},
            },
        ]
        store = type("FakeStore", (), {})()
        mgr = ResumeManager(state_store=store, trace_dir=str(tmp_path))
        msgs, incomplete = mgr._reconstruct_messages(entries)

        assert len(msgs) == 2
        assert msgs[0].role == "user"
        assert msgs[0].content == "hello"
        assert msgs[1].role == "assistant"
        assert msgs[1].content == "hi there"
        assert len(incomplete) == 0

    def test_reconstruct_with_incomplete_tool(self, tmp_path):
        from dotclaw.agent.resume import ResumeManager

        entries: list[dict] = [
            {
                "ts": 1.0, "t": "00:00:01.000", "type": "trace.message",
                "agentrun_id": "run-001",
                "data": {"role": "user", "content": "search for x"},
            },
            {
                "ts": 2.0, "t": "00:00:02.000", "type": "trace.message",
                "agentrun_id": "run-001",
                "data": {
                    "role": "assistant", "content": "",
                    "tool_calls": [
                        {"id": "c1", "name": "search", "arguments": '{"q":"x"}'},
                        {"id": "c2", "name": "read", "arguments": '{}'},
                    ],
                },
            },
        ]
        store = type("FakeStore", (), {})()
        mgr = ResumeManager(state_store=store, trace_dir=str(tmp_path))
        msgs, incomplete = mgr._reconstruct_messages(entries)

        assert len(msgs) == 2
        assert len(incomplete) == 2
        assert incomplete[0].id == "c1"
        assert incomplete[1].id == "c2"

    def test_reconstruct_with_partial_tool_result(self, tmp_path):
        from dotclaw.agent.resume import ResumeManager

        entries: list[dict] = [
            {
                "ts": 1.0, "t": "00:00:01.000", "type": "trace.message",
                "agentrun_id": "run-001",
                "data": {"role": "user", "content": "search"},
            },
            {
                "ts": 2.0, "t": "00:00:02.000", "type": "trace.message",
                "agentrun_id": "run-001",
                "data": {
                    "role": "assistant", "content": "",
                    "tool_calls": [
                        {"id": "c1", "name": "a", "arguments": "{}"},
                        {"id": "c2", "name": "b", "arguments": "{}"},
                    ],
                },
            },
            {
                "ts": 3.0, "t": "00:00:03.000", "type": "trace.message",
                "agentrun_id": "run-001",
                "data": {
                    "role": "tool", "content": "result-a",
                    "tool_call_id": "c1", "name": "a",
                },
            },
        ]
        store = type("FakeStore", (), {})()
        mgr = ResumeManager(state_store=store, trace_dir=str(tmp_path))
        msgs, incomplete = mgr._reconstruct_messages(entries)

        # c1 有 tool_result，c2 没有
        assert len(incomplete) == 1
        assert incomplete[0].id == "c2"

    def test_skips_non_trace_message_events(self, tmp_path):
        from dotclaw.agent.resume import ResumeManager

        entries: list[dict] = [
            {
                "ts": 1.0, "t": "00:00:01.000", "type": "session.start",
                "data": {"session_id": "s1"},
            },
            {
                "ts": 2.0, "t": "00:00:02.000", "type": "trace.message",
                "agentrun_id": "run-001",
                "data": {"role": "user", "content": "hi"},
            },
            {
                "ts": 3.0, "t": "00:00:03.000", "type": "llm.call_start",
                "data": {"model": "test"},
            },
        ]
        store = type("FakeStore", (), {})()
        mgr = ResumeManager(state_store=store, trace_dir=str(tmp_path))
        msgs, incomplete = mgr._reconstruct_messages(entries)

        # 只应该重建一条 trace.message 事件
        assert len(msgs) == 1
        assert msgs[0].content == "hi"
