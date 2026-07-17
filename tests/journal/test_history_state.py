"""Journal trace recording 测试。

测试 Journal.record_message() 以 TRACE_MESSAGE 事件写入 trace.jsonl，
旧 Journal StateSink 覆盖写入 state.json 的历史兼容测试。
v2：产出路径统一为 session/{session_id}/
"""

import json
import time
from pathlib import Path

import pytest

from dotclaw.journal.journal import Journal
from dotclaw.llm.base import Message, ToolCall


# ═══════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════

def _make_config(tmp_path: str, **overrides):
    """创建最小 JournalConfig，所有开关默认关闭。"""
    from dotclaw.config.settings import JournalConfig
    defaults: dict = {
        "trace_dir": tmp_path,
        "snapshot_dir": tmp_path,
        "console": False,
        "trace": False,
        "snapshot": False,
        "history": True,
        "state": False,
    }
    defaults.update(overrides)
    return JournalConfig(**defaults)


def _start_session(journal: Journal, tmp_path: str, **overrides):
    """快速启动 session 并注入配置。"""
    config = _make_config(tmp_path, **overrides)
    journal.session_start(
        session_id="s-test",
        model="test-model",
        config=config,
    )


# ═══════════════════════════════════════════════════════════════════
# HistorySink (still functional, used indirectly via Journal)
# ═══════════════════════════════════════════════════════════════════

class TestHistorySink:
    """HistorySink 追加写入文件。"""

    def test_creates_file_on_first_write(self, tmp_path):
        from dotclaw.journal.sinks.history_sink import HistorySink
        sink = HistorySink(Path(tmp_path) / "test.jsonl")
        sink.write({"loop": 0, "content": "hello"})
        sink.close()

        filepath = Path(tmp_path) / "test.jsonl"
        assert filepath.exists()

    def test_appends_json_lines(self, tmp_path):
        from dotclaw.journal.sinks.history_sink import HistorySink
        sink = HistorySink(Path(tmp_path) / "test.jsonl")
        sink.write({"loop": 0, "content": "hello"})
        sink.write({"loop": 0, "content": "result"})
        sink.close()

        lines = Path(tmp_path).joinpath("test.jsonl").read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 2
        assert json.loads(lines[0])["content"] == "hello"
        assert json.loads(lines[1])["content"] == "result"

    def test_write_handles_non_ascii(self, tmp_path):
        from dotclaw.journal.sinks.history_sink import HistorySink
        sink = HistorySink(Path(tmp_path) / "test.jsonl")
        sink.write({"loop": 0, "content": "你好，今天天气不错！"})
        sink.close()

        lines = Path(tmp_path).joinpath("test.jsonl").read_text(encoding="utf-8").strip().split("\n")
        assert "你好" in lines[0]

    def test_close_flushes_and_closes(self, tmp_path):
        from dotclaw.journal.sinks.history_sink import HistorySink
        sink = HistorySink(Path(tmp_path) / "test.jsonl")
        sink.write({"loop": 0, "content": "before close"})
        sink.close()

        content = Path(tmp_path).joinpath("test.jsonl").read_text(encoding="utf-8")
        assert "before close" in content


# ═══════════════════════════════════════════════════════════════════
# StateSink
# ═══════════════════════════════════════════════════════════════════

class TestStateSink:
    """StateSink 原子覆盖写入 state.json。"""

    def test_writes_state_file(self, tmp_path):
        from dotclaw.journal.sinks.state_sink import StateSink
        sink = StateSink(Path(tmp_path) / "state.json")
        sink.write({"session_id": "s-test", "status": "running"})

        filepath = Path(tmp_path) / "state.json"
        assert filepath.exists()
        data = json.loads(filepath.read_text(encoding="utf-8"))
        assert data["session_id"] == "s-test"

    def test_overwrite_replaces_content(self, tmp_path):
        from dotclaw.journal.sinks.state_sink import StateSink
        filepath = Path(tmp_path) / "state.json"
        sink = StateSink(filepath)
        sink.write({"status": "running"})
        sink.write({"status": "completed"})

        data = json.loads(filepath.read_text(encoding="utf-8"))
        assert data["status"] == "completed"

    def test_atomic_write_no_temp_leftover(self, tmp_path):
        from dotclaw.journal.sinks.state_sink import StateSink
        sink = StateSink(Path(tmp_path) / "state.json")
        sink.write({"status": "running"})

        tmp_files = list(Path(tmp_path).glob("*.tmp"))
        assert len(tmp_files) == 0

    def test_missing_parent_dir_is_created(self, tmp_path):
        from dotclaw.journal.sinks.state_sink import StateSink
        nested = Path(tmp_path) / "deep" / "nested" / "state.json"
        sink = StateSink(nested)
        sink.write({"status": "ok"})
        assert nested.exists()


# ═══════════════════════════════════════════════════════════════════
# Journal record_message (now as TRACE_MESSAGE in trace.jsonl)
# ═══════════════════════════════════════════════════════════════════

class TestJournalRecordMessage:
    """Journal.record_message() 集成测试。v2: 写入 trace.jsonl。"""

    def test_record_message_emits_trace_message_event(self, tmp_path):
        journal = Journal()
        _start_session(journal, str(tmp_path), trace=True)
        journal.agentrun_start("run-001", "user_input")

        msg = Message(role="assistant", content="hi")
        journal.record_message(msg)
        journal.agentrun_end("completed")

        # 验证事件中包含了 TRACE_MESSAGE
        trace_events = [e for e in journal._events if e.event_type == "trace.message"]
        assert len(trace_events) == 1
        assert trace_events[0].data["role"] == "assistant"
        assert trace_events[0].data["content"] == "hi"

    def test_record_before_session_is_noop(self):
        journal = Journal()
        msg = Message(role="assistant", content="hi")
        journal.record_message(msg)
        # 不应抛异常

    def test_multiple_messages_in_one_session(self, tmp_path):
        journal = Journal()
        _start_session(journal, str(tmp_path), trace=True)
        journal.agentrun_start("run-001", "user_input")

        journal.record_message(Message(role="user", content="query"))
        journal.record_message(Message(
            role="assistant", content="resp",
            tool_calls=[ToolCall(id="c1", name="search", arguments='{"q":"x"}')],
        ))
        journal.record_message(Message(role="tool", content="result", tool_call_id="c1", name="search"))

        trace_events = [e for e in journal._events if e.event_type == "trace.message"]
        assert len(trace_events) == 3

        # 验证 tool_calls 被正确序列化
        asst_event = trace_events[1]
        assert asst_event.data["tool_calls"] == [
            {"id": "c1", "name": "search", "arguments": '{"q":"x"}'},
        ]

        tool_event = trace_events[2]
        assert tool_event.data["tool_call_id"] == "c1"
        assert tool_event.data["name"] == "search"

    def test_record_message_with_agentrun_id(self, tmp_path):
        """验证每个 TRACE_MESSAGE 携带 agentrun_id。"""
        journal = Journal()
        _start_session(journal, str(tmp_path), trace=True)
        journal.agentrun_start("run-001", "user_input")
        journal.record_message(Message(role="user", content="hello"))

        # 通过 _write_trace_line 写入的 trace.jsonl 文件
        trace_dir = Path(tmp_path) / "s-test"
        trace_path = trace_dir / "trace.jsonl"
        assert trace_path.exists()

        lines = trace_path.read_text(encoding="utf-8").strip().split("\n")
        entries = [json.loads(l) for l in lines if l.strip()]
        trace_messages = [e for e in entries if e.get("type") == "trace.message"]
        assert len(trace_messages) == 1
        assert trace_messages[0].get("agentrun_id") == "run-001"


# ═══════════════════════════════════════════════════════════════════
# Journal _update_state
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.legacy
class TestJournalUpdateState:
    """Journal._update_state() 集成测试。"""

    def test_update_state_writes_required_fields(self, tmp_path):
        journal = Journal()
        _start_session(journal, str(tmp_path), state=True)
        journal.agentrun_start("run-001", "user_input")

        journal._token_accum = {"input": 3200, "output": 500}
        journal._tool_count = 3
        journal._errors_list = []
        journal._max_loop_steps = 10
        journal._update_state("running")

        state_dir = Path(tmp_path) / "s-test"
        state_path = state_dir / "state.json"
        assert state_path.exists()

        data = json.loads(state_path.read_text(encoding="utf-8"))
        assert data["session_id"] == "s-test"
        assert data["agentrun_id"] == "run-001"
        assert data["status"] == "running"
        assert data["total_input_tokens"] == 3200
        assert data["total_output_tokens"] == 500
        assert data["total_tool_calls"] == 3
        assert data["model"] == "test-model"
        assert data["max_loop_steps"] == 10
        assert "updated_at" in data

    def test_state_transitions_on_session_end(self, tmp_path):
        journal = Journal()
        _start_session(journal, str(tmp_path), state=True)
        journal._token_accum = {"input": 1000, "output": 200}
        journal._tool_count = 2
        journal._errors_list = []
        journal._max_loop_steps = 5

        journal._update_state("completed")

        state_path = Path(tmp_path) / "s-test" / "state.json"
        data = json.loads(state_path.read_text(encoding="utf-8"))
        assert data["status"] == "completed"

    def test_state_disabled_does_not_create_file(self, tmp_path):
        journal = Journal()
        _start_session(journal, str(tmp_path), state=False)
        journal._update_state("running")

        state_path = Path(tmp_path) / "s-test" / "state.json"
        assert not state_path.exists()

    def test_errors_tracked_in_state(self, tmp_path):
        journal = Journal()
        _start_session(journal, str(tmp_path), state=True)
        journal._errors_list = [{"source": "tool.execute", "message": "timeout"}]
        journal._update_state("error")

        state_path = Path(tmp_path) / "s-test" / "state.json"
        data = json.loads(state_path.read_text(encoding="utf-8"))
        assert data["status"] == "error"
        assert len(data["errors"]) == 1
        assert data["errors"][0]["source"] == "tool.execute"


# ═══════════════════════════════════════════════════════════════════
# Output directory path
# ═══════════════════════════════════════════════════════════════════

class TestOutputPath:
    """v2 产出路径。"""

    def test_output_dir_includes_session_id(self, tmp_path):
        journal = Journal()
        _start_session(journal, str(tmp_path))

        out_dir = journal._trace_output_dir()
        assert out_dir is not None
        assert str(out_dir).endswith("s-test")
        assert "s-test" in str(out_dir)

    def test_output_dir_format(self, tmp_path):
        journal = Journal()
        _start_session(journal, str(tmp_path))

        out_dir = journal._trace_output_dir()
        # 路径: {trace_dir}/{session_id}/
        assert out_dir.name == "s-test"

    def test_output_dir_none_before_session_start(self):
        journal = Journal()
        assert journal._trace_output_dir() is None

    def test_output_dir_creates_directory(self, tmp_path):
        journal = Journal()
        _start_session(journal, str(tmp_path))

        out_dir = journal._trace_output_dir()
        assert out_dir is not None
        out_dir.mkdir(parents=True, exist_ok=True)
        assert out_dir.exists()
        assert out_dir.is_dir()
