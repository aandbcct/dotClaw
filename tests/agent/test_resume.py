"""Agent Resume 功能测试。

测试 ResumeManager 的 find_interrupted / load_history / reconstruct / resolve。
"""

import json
from pathlib import Path

import pytest

from dotclaw.llm.base import Message, ToolCall


# ═══════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════

def _write_trace(session_dir: Path, request_subdir: str,
                 state: dict, history_lines: list[dict]) -> Path:
    """在测试临时目录下构造一个完整的 trace 目录。"""
    from datetime import date
    date_str = date.today().isoformat()
    trace_dir = session_dir / date_str / request_subdir
    trace_dir.mkdir(parents=True, exist_ok=True)

    (trace_dir / "state.json").write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    if history_lines:
        (trace_dir / "history.jsonl").write_text(
            "\n".join(json.dumps(line, ensure_ascii=False) for line in history_lines),
            encoding="utf-8")
    return trace_dir


# ═══════════════════════════════════════════════════════════════════
# find_interrupted
# ═══════════════════════════════════════════════════════════════════

class TestFindInterrupted:
    """检测中断的 request。"""

    def test_finds_running_state(self, tmp_path):
        from dotclaw.agent.resume import ResumeManager
        session_dir = tmp_path / "s-test"
        _write_trace(session_dir, "123456-req-001",
                     {"status": "running", "loop_index": 2}, [])

        mgr = ResumeManager(trace_root=str(tmp_path))
        found = mgr.find_interrupted("s-test")
        assert found is not None
        assert "req-001" in str(found)

    def test_ignores_completed(self, tmp_path):
        from dotclaw.agent.resume import ResumeManager
        session_dir = tmp_path / "s-test"
        _write_trace(session_dir, "123456-req-001",
                     {"status": "completed"}, [])

        mgr = ResumeManager(trace_root=str(tmp_path))
        found = mgr.find_interrupted("s-test")
        assert found is None

    def test_picks_latest_if_multiple_running(self, tmp_path):
        from dotclaw.agent.resume import ResumeManager
        session_dir = tmp_path / "s-test"
        _write_trace(session_dir, "123456-req-old",
                     {"status": "running"}, [])
        _write_trace(session_dir, "223456-req-new",
                     {"status": "running"}, [])

        mgr = ResumeManager(trace_root=str(tmp_path))
        found = mgr.find_interrupted("s-test")
        assert found is not None
        assert "req-new" in str(found)

    def test_no_session_returns_none(self, tmp_path):
        from dotclaw.agent.resume import ResumeManager
        mgr = ResumeManager(trace_root=str(tmp_path))
        found = mgr.find_interrupted("nonexistent")
        assert found is None


# ═══════════════════════════════════════════════════════════════════
# load_history
# ═══════════════════════════════════════════════════════════════════

class TestLoadHistory:
    """从 history.jsonl 加载条目。"""

    def test_loads_all_lines(self, tmp_path):
        from dotclaw.agent.resume import ResumeManager
        mgr = ResumeManager()

        p = tmp_path / "h.jsonl"
        p.write_text('\n'.join([
            json.dumps({"loop": -1, "step": "user_input", "role": "user", "content": "hi"}),
            json.dumps({"loop": 0, "step": "llm_response", "role": "assistant", "content": "hello"}),
        ]), encoding="utf-8")

        entries = mgr.load_history(p)
        assert len(entries) == 2
        assert entries[0]["step"] == "user_input"

    def test_empty_file_returns_empty(self, tmp_path):
        from dotclaw.agent.resume import ResumeManager
        mgr = ResumeManager()

        p = tmp_path / "empty.jsonl"
        p.write_text("", encoding="utf-8")

        entries = mgr.load_history(p)
        assert entries == []

    def test_missing_file_returns_empty(self, tmp_path):
        from dotclaw.agent.resume import ResumeManager
        mgr = ResumeManager()

        entries = mgr.load_history(tmp_path / "missing.jsonl")
        assert entries == []


# ═══════════════════════════════════════════════════════════════════
# reconstruct
# ═══════════════════════════════════════════════════════════════════

class TestReconstruct:
    """从 history 条目重建 Message 列表，检测未完成工具。"""

    def test_reconstruct_simple_conversation(self):
        from dotclaw.agent.resume import ResumeManager
        mgr = ResumeManager()
        entries = [
            {"loop": -1, "step": "user_input", "role": "user", "content": "hi"},
            {"loop": 0, "step": "llm_response", "role": "assistant",
             "content": "hello", "tool_calls": None},
        ]

        messages, incomplete = mgr.reconstruct(entries)
        assert len(messages) == 2
        assert messages[0].role == "user"
        assert messages[0].content == "hi"
        assert messages[1].role == "assistant"
        assert messages[1].content == "hello"
        assert incomplete == []

    def test_reconstruct_with_tool_calls_and_results(self):
        from dotclaw.agent.resume import ResumeManager
        mgr = ResumeManager()
        entries = [
            {"loop": -1, "step": "user_input", "role": "user", "content": "search"},
            {"loop": 0, "step": "llm_response", "role": "assistant",
             "content": "", "tool_calls": [
                 {"id": "c1", "name": "search", "args": '{"q":"x"}'},
             ]},
            {"loop": 0, "step": "tool_result", "role": "tool",
             "content": "found", "tool_call_id": "c1", "name": "search"},
            {"loop": 1, "step": "llm_response", "role": "assistant",
             "content": "result: found", "tool_calls": None},
        ]

        messages, incomplete = mgr.reconstruct(entries)
        assert len(messages) == 4
        assert messages[0].role == "user"
        assert messages[1].role == "assistant"
        assert len(messages[1].tool_calls) == 1
        assert messages[1].tool_calls[0].id == "c1"
        assert messages[2].role == "tool"
        assert messages[2].tool_call_id == "c1"
        assert incomplete == []

    def test_reconstruct_detects_incomplete_tool(self):
        from dotclaw.agent.resume import ResumeManager
        mgr = ResumeManager()
        entries = [
            {"loop": -1, "step": "user_input", "role": "user", "content": "do all"},
            {"loop": 0, "step": "llm_response", "role": "assistant",
             "content": "", "tool_calls": [
                 {"id": "c1", "name": "search", "args": '{}'},
                 {"id": "c2", "name": "write", "args": '{}'},
             ]},
            {"loop": 0, "step": "tool_result", "role": "tool",
             "content": "done", "tool_call_id": "c1", "name": "search"},
            # c2 缺少 tool_result
        ]

        messages, incomplete = mgr.reconstruct(entries)
        assert len(messages) == 3  # user + assistant + c1 的结果
        assert len(incomplete) == 1
        assert incomplete[0].id == "c2"
        assert incomplete[0].name == "write"

    def test_reconstruct_all_tools_incomplete(self):
        from dotclaw.agent.resume import ResumeManager
        mgr = ResumeManager()
        entries = [
            {"loop": -1, "step": "user_input", "role": "user", "content": "do"},
            {"loop": 0, "step": "llm_response", "role": "assistant",
             "content": "", "tool_calls": [
                 {"id": "c1", "name": "search", "args": '{}'},
             ]},
            # 没有任何 tool_result
        ]

        messages, incomplete = mgr.reconstruct(entries)
        assert len(messages) == 2  # user + assistant
        assert len(incomplete) == 1
        assert incomplete[0].id == "c1"


# ═══════════════════════════════════════════════════════════════════
# ResumeManager integration
# ═══════════════════════════════════════════════════════════════════

class TestResumeManagerIntegration:
    """端到端 resume 流程。"""

    def test_resume_no_interrupted_request(self, tmp_path):
        from dotclaw.agent.resume import ResumeManager
        mgr = ResumeManager(trace_root=str(tmp_path))

        result = mgr.get_resume_context("s-test")
        assert result is None

    def test_resume_context_contains_messages_and_incomplete(self, tmp_path):
        from dotclaw.agent.resume import ResumeManager

        session_dir = tmp_path / "s-test"
        _write_trace(session_dir, "120000-req-int",
                     {"status": "running", "loop_index": 0,
                      "total_input_tokens": 500, "total_output_tokens": 50}, [
            {"loop": -1, "step": "user_input", "role": "user", "content": "do it"},
            {"loop": 0, "step": "llm_response", "role": "assistant",
             "content": "", "tool_calls": [
                 {"id": "c1", "name": "search", "args": '{}'},
                 {"id": "c2", "name": "write", "args": '{}'},
             ]},
            {"loop": 0, "step": "tool_result", "role": "tool",
             "content": "found", "tool_call_id": "c1", "name": "search"},
        ])

        mgr = ResumeManager(trace_root=str(tmp_path))
        ctx = mgr.get_resume_context("s-test")
        assert ctx is not None
        assert len(ctx["messages"]) == 3
        assert len(ctx["incomplete_tools"]) == 1
        assert ctx["incomplete_tools"][0].name == "write"
        assert ctx["request_id"] == "req-int"
        assert ctx["state"]["loop_index"] == 0
        assert ctx["state"]["total_input_tokens"] == 500

    def test_resume_context_none_for_completed(self, tmp_path):
        from dotclaw.agent.resume import ResumeManager

        session_dir = tmp_path / "s-test"
        _write_trace(session_dir, "120000-req-done",
                     {"status": "completed"}, [
            {"loop": -1, "step": "user_input", "role": "user", "content": "done"},
        ])

        mgr = ResumeManager(trace_root=str(tmp_path))
        ctx = mgr.get_resume_context("s-test")
        assert ctx is None


# ═══════════════════════════════════════════════════════════════════
# Journal restore_state
# ═══════════════════════════════════════════════════════════════════

class TestJournalRestoreState:
    """Journal.restore_state() 恢复累加器。"""

    def test_restores_loop_idx(self):
        from dotclaw.journal.journal import Journal
        journal = Journal()
        journal._loop_idx = -1
        journal.restore_state({"loop_index": 3})
        assert journal._loop_idx == 3

    def test_restores_all_accumulators(self):
        from dotclaw.journal.journal import Journal
        journal = Journal()
        journal.restore_state({
            "loop_index": 2,
            "total_input_tokens": 3200,
            "total_output_tokens": 500,
            "total_tool_calls": 3,
            "errors": [{"source": "tool", "message": "timeout"}],
            "message_count": 8,
        })
        assert journal._loop_idx == 2
        assert journal._token_accum["input"] == 3200
        assert journal._token_accum["output"] == 500
        assert journal._tool_count == 3
        assert len(journal._errors_list) == 1
        assert journal._message_count == 8

    def test_restores_missing_fields_as_defaults(self):
        from dotclaw.journal.journal import Journal
        journal = Journal()
        journal.restore_state({})  # 空 state
        assert journal._loop_idx == -1
        assert journal._token_accum["input"] == 0
