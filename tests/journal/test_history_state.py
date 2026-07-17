"""Journal trace recording 测试。

测试 Journal.record_message() 以 TRACE_MESSAGE 事件写入 trace.jsonl。
Runtime v2 的运行事实和恢复状态由 RunRepository、CheckpointRepository 管理。
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
