"""Journal history/state recording 测试。

测试 HistorySink、StateSink、Journal.record_message()、Journal._update_state()
以及新的产出路径格式 {trace_dir}/{session_id}/{date}/{HHMMSS}-{request_id}。
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
    defaults = {
        "trace_dir": tmp_path,
        "snapshot_dir": tmp_path,
        "console": False,
        "trace": False,
        "snapshot": False,
        "history": False,
        "state": False,
    }
    defaults.update(overrides)
    return JournalConfig(**defaults)


def _start_session(journal: Journal, tmp_path: str, **overrides):
    """快速启动 session 并注入配置。"""
    config = _make_config(tmp_path, **overrides)
    journal.session_start(
        session_id="s-test",
        request_id="req-001",
        model="test-model",
        config=config,
    )


# ═══════════════════════════════════════════════════════════════════
# HistorySink
# ═══════════════════════════════════════════════════════════════════

class TestHistorySink:
    """HistorySink 追加写入 history.jsonl。"""

    def test_creates_file_on_first_write(self, tmp_path):
        from dotclaw.journal.sinks.history_sink import HistorySink
        sink = HistorySink(Path(tmp_path) / "history.jsonl")
        sink.write({"loop": 0, "step": "llm_response", "content": "hello"})
        sink.close()

        filepath = Path(tmp_path) / "history.jsonl"
        assert filepath.exists()

    def test_appends_json_lines(self, tmp_path):
        from dotclaw.journal.sinks.history_sink import HistorySink
        sink = HistorySink(Path(tmp_path) / "history.jsonl")
        sink.write({"loop": 0, "step": "llm_response", "content": "hello"})
        sink.write({"loop": 0, "step": "tool_result", "content": "result"})
        sink.close()

        lines = Path(tmp_path).joinpath("history.jsonl").read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 2
        assert json.loads(lines[0])["content"] == "hello"
        assert json.loads(lines[1])["content"] == "result"

    def test_write_handles_non_ascii(self, tmp_path):
        from dotclaw.journal.sinks.history_sink import HistorySink
        sink = HistorySink(Path(tmp_path) / "history.jsonl")
        sink.write({"loop": 0, "step": "llm_response", "content": "你好，今天天气不错！"})
        sink.close()

        lines = Path(tmp_path).joinpath("history.jsonl").read_text(encoding="utf-8").strip().split("\n")
        assert "你好" in lines[0]

    def test_close_flushes_and_closes(self, tmp_path):
        from dotclaw.journal.sinks.history_sink import HistorySink
        sink = HistorySink(Path(tmp_path) / "history.jsonl")
        sink.write({"loop": 0, "step": "llm_response", "content": "before close"})
        sink.close()

        # 文件此时应存在且可读
        content = Path(tmp_path).joinpath("history.jsonl").read_text(encoding="utf-8")
        assert "before close" in content


# ═══════════════════════════════════════════════════════════════════
# StateSink
# ═══════════════════════════════════════════════════════════════════

class TestStateSink:
    """StateSink 原子覆盖写入 state.json。"""

    def test_writes_state_file(self, tmp_path):
        from dotclaw.journal.sinks.state_sink import StateSink
        sink = StateSink(Path(tmp_path) / "state.json")
        sink.write({"session_id": "s-test", "loop_index": 1, "status": "running"})

        filepath = Path(tmp_path) / "state.json"
        assert filepath.exists()
        data = json.loads(filepath.read_text(encoding="utf-8"))
        assert data["session_id"] == "s-test"
        assert data["loop_index"] == 1

    def test_overwrite_replaces_content(self, tmp_path):
        from dotclaw.journal.sinks.state_sink import StateSink
        filepath = Path(tmp_path) / "state.json"
        sink = StateSink(filepath)
        sink.write({"status": "running", "loop_index": 1})
        sink.write({"status": "completed", "loop_index": 3})

        data = json.loads(filepath.read_text(encoding="utf-8"))
        assert data["status"] == "completed"
        assert data["loop_index"] == 3

    def test_atomic_write_no_temp_leftover(self, tmp_path):
        from dotclaw.journal.sinks.state_sink import StateSink
        sink = StateSink(Path(tmp_path) / "state.json")
        sink.write({"status": "running"})

        # 不应遗留 .tmp 文件
        tmp_files = list(Path(tmp_path).glob("*.tmp"))
        assert len(tmp_files) == 0

    def test_missing_parent_dir_is_created(self, tmp_path):
        from dotclaw.journal.sinks.state_sink import StateSink
        nested = Path(tmp_path) / "deep" / "nested" / "state.json"
        sink = StateSink(nested)
        sink.write({"status": "ok"})
        assert nested.exists()


# ═══════════════════════════════════════════════════════════════════
# Journal record_message
# ═══════════════════════════════════════════════════════════════════

class TestJournalRecordMessage:
    """Journal.record_message() 集成测试。"""

    def test_record_message_writes_to_file(self, tmp_path):
        journal = Journal()
        _start_session(journal, str(tmp_path), history=True)
        msg = Message(role="assistant", content="hi")
        journal.record_message(msg)
        journal.finalize()

        # 路径: {trace_dir}/{session_id}/{date}/{HHMMSS}-{request_id}/history.jsonl
        from datetime import date
        date_str = date.today().isoformat()
        # 子目录包含 HHMMSS- 时间前缀
        out_dir = list((Path(tmp_path) / "s-test" / date_str).iterdir())[0]
        history_path = out_dir / "history.jsonl"
        assert history_path.exists()
        lines = history_path.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 1

    def test_record_before_session_is_noop(self):
        journal = Journal()
        msg = Message(role="assistant", content="hi")
        journal.record_message(msg)
        # 不应抛异常

    def test_history_disabled_does_not_create_file(self, tmp_path):
        journal = Journal()
        _start_session(journal, str(tmp_path), history=False)
        msg = Message(role="assistant", content="hi")
        journal.record_message(msg)
        journal.finalize()

        from datetime import date
        date_str = date.today().isoformat()
        # 即使 disabled，目录也可能不存在
        s_dir = Path(tmp_path) / "s-test" / date_str
        if s_dir.exists():
            for d in s_dir.iterdir():
                history_path = d / "history.jsonl"
                assert not history_path.exists()

    def test_multiple_messages_in_one_session(self, tmp_path):
        journal = Journal()
        _start_session(journal, str(tmp_path), history=True)

        # 用户输入 (loop=-1)
        journal.record_message(Message(role="user", content="query"))
        # 模拟第一次 loop
        journal.loop_start()
        # LLM 返回
        journal.record_message(Message(
            role="assistant", content="resp",
            tool_calls=[ToolCall(id="c1", name="search", arguments='{"q":"x"}')],
        ))
        # 工具结果
        journal.record_message(Message(role="tool", content="result", tool_call_id="c1", name="search"))
        journal.finalize()

        from datetime import date
        date_str = date.today().isoformat()
        out_dir = list((Path(tmp_path) / "s-test" / date_str).iterdir())[0]
        history_path = out_dir / "history.jsonl"
        lines = history_path.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 3

        # 验证 loop 值：用户输入=-1, LLM=0, tool=0
        data = [json.loads(l) for l in lines]
        assert data[0]["loop"] == -1
        assert data[0]["step"] == "user_input"
        assert data[1]["loop"] == 0
        assert data[1]["step"] == "llm_response"
        assert data[1]["tool_calls"] == [{"id": "c1", "name": "search", "args": '{"q":"x"}'}]
        assert data[2]["loop"] == 0
        assert data[2]["step"] == "tool_result"
        assert data[2]["tool_call_id"] == "c1"

    def test_record_message_injects_loop_and_ts(self, tmp_path):
        """验证 loop/ts 由 Journal 注入，无需外部传。"""
        journal = Journal()
        _start_session(journal, str(tmp_path), history=True)
        # 不传 loop/ts——Journal 自己加
        journal.record_message(Message(role="user", content="hello"))
        journal.finalize()

        from datetime import date
        date_str = date.today().isoformat()
        out_dir = list((Path(tmp_path) / "s-test" / date_str).iterdir())[0]
        history_path = out_dir / "history.jsonl"
        entry = json.loads(history_path.read_text(encoding="utf-8").strip())
        assert entry["loop"] == -1
        assert "ts" in entry
        assert entry["role"] == "user"
        assert entry["content"] == "hello"


# ═══════════════════════════════════════════════════════════════════
# Journal _update_state
# ═══════════════════════════════════════════════════════════════════

class TestJournalUpdateState:
    """Journal._update_state() 集成测试。"""

    def test_update_state_writes_required_fields(self, tmp_path):
        journal = Journal()
        _start_session(journal, str(tmp_path), state=True)

        # 模拟一轮 loop 后的状态
        journal._loop_idx = 1
        journal._token_accum = {"input": 3200, "output": 500}
        journal._tool_count = 3
        journal._errors_list = []
        journal._max_loop_steps = 10
        journal._update_state("running")

        from datetime import date
        date_str = date.today().isoformat()
        out_dir = list((Path(tmp_path) / "s-test" / date_str).iterdir())[0]
        state_path = out_dir / "state.json"
        assert state_path.exists()

        data = json.loads(state_path.read_text(encoding="utf-8"))
        assert data["session_id"] == "s-test"
        assert data["request_id"] == "req-001"
        assert data["loop_index"] == 1
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
        journal._loop_idx = 3
        journal._token_accum = {"input": 1000, "output": 200}
        journal._tool_count = 2
        journal._errors_list = []
        journal._max_loop_steps = 5

        # 模拟 session_end 调用
        journal._update_state("completed")

        from datetime import date
        date_str = date.today().isoformat()
        out_dir = list((Path(tmp_path) / "s-test" / date_str).iterdir())[0]
        state_path = out_dir / "state.json"
        data = json.loads(state_path.read_text(encoding="utf-8"))
        assert data["status"] == "completed"

    def test_state_disabled_does_not_create_file(self, tmp_path):
        journal = Journal()
        _start_session(journal, str(tmp_path), state=False)
        journal._update_state("running")

        from datetime import date
        date_str = date.today().isoformat()
        state_path = Path(tmp_path) / "s-test" / date_str / "req-001" / "state.json"
        assert not state_path.exists()

    def test_errors_tracked_in_state(self, tmp_path):
        journal = Journal()
        _start_session(journal, str(tmp_path), state=True)
        journal._errors_list = [{"source": "tool.execute", "message": "timeout"}]
        journal._update_state("error")

        from datetime import date
        date_str = date.today().isoformat()
        out_dir = list((Path(tmp_path) / "s-test" / date_str).iterdir())[0]
        state_path = out_dir / "state.json"
        data = json.loads(state_path.read_text(encoding="utf-8"))
        assert data["status"] == "error"
        assert len(data["errors"]) == 1
        assert data["errors"][0]["source"] == "tool.execute"


# ═══════════════════════════════════════════════════════════════════
# Output directory path (with session_id isolation)
# ═══════════════════════════════════════════════════════════════════

class TestOutputPath:
    """新的产出路径包含 session_id 做会话隔离。"""

    def test_output_dir_includes_session_id(self, tmp_path):
        journal = Journal()
        _start_session(journal, str(tmp_path))

        out_dir = journal._output_dir()
        assert out_dir is not None
        assert "s-test" in str(out_dir)

    def test_output_dir_format(self, tmp_path):
        journal = Journal()
        _start_session(journal, str(tmp_path))

        out_dir = journal._output_dir()
        from datetime import date
        date_str = date.today().isoformat()

        # 路径: {trace_dir}/{session_id}/{date}/{HHMMSS}-{request_id}
        dir_name = out_dir.name
        assert dir_name.endswith("-req-001")
        assert "s-test" in str(out_dir)
        assert date_str in str(out_dir)
        # 验证 HHMMSS 格式（6 位数字 + "-" 前缀）
        prefix = dir_name.split("-")[0]
        assert len(prefix) == 6
        assert prefix.isdigit()

    def test_output_dir_none_before_session_start(self):
        journal = Journal()
        assert journal._output_dir() is None

    def test_output_dir_creates_directory(self, tmp_path):
        journal = Journal()
        _start_session(journal, str(tmp_path))

        out_dir = journal._ensure_output_dir()
        assert out_dir.exists()
        assert out_dir.is_dir()
