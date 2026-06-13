"""Journal 核心 API 测试。

测试 Journal 的事件发射、参数内化、生命周期。
"""

import json
import os
import tempfile
import time
from pathlib import Path

import pytest

from dotclaw.journal.journal import Journal
from dotclaw.journal.events import AgentEvent, EventType


# ═══════════════════════════════════════════════════════════════════
# Mock AgentContext for testing
# ═══════════════════════════════════════════════════════════════════

@pytest.fixture
def fake_context():
    """创建一个最小可用的 AgentContext 模拟。"""
    from dataclasses import dataclass, field

    @dataclass(frozen=True)
    class FakeContext:
        session_id: str = "test-session-001"
        request_id: str = "req-abcd1234"
        model: str = "deepseek-v4"
        workspace: str = "."
        project_root: str = "."
        system_prompt: str = ""
        available_tools: list = field(default_factory=list)
        tool_definitions: list = field(default_factory=list)
        purpose: str = "chat"
        max_context_tokens: int = 8000
        rules: str = ""
        created_at: str = ""
        channel: None = None
        memory_summary: str = ""
        skill_registry: None = None
        journal: None = None

    return FakeContext()


@pytest.fixture
def journal_config():
    """创建一个最小可用的 JournalConfig。"""
    from dotclaw.config.settings import JournalConfig

    return JournalConfig(
        trace_dir="/tmp/test_traces",
        snapshot_dir="/tmp/test_snapshots",
        console=False,   # 测试中关闭控制台输出
        trace=False,     # 测试中关闭文件写入
        snapshot=False,  # 测试中关闭快照保存
    )


# ═══════════════════════════════════════════════════════════════════
# Session 测试
# ═══════════════════════════════════════════════════════════════════


class TestSessionStart:
    """测试 session_start() 从 AgentContext 提取上下文。"""

    def test_session_start_pulls_from_context(self, fake_context, journal_config):
        journal = Journal()
        journal.session_start(fake_context, journal_config)

        assert journal._session_id == "test-session-001"
        assert journal._request_id == "req-abcd1234"
        assert journal._model == "deepseek-v4"
        assert journal._loop_idx == 0
        assert len(journal._events) == 1
        assert journal._events[0].event_type == "session.start"

    def test_session_start_initializes_loop_idx(self, fake_context, journal_config):
        journal = Journal()
        journal.session_start(fake_context, journal_config)
        assert journal._loop_idx == 0


class TestSessionEnd:
    """测试 session_end() 发射结束事件。"""

    def test_session_end_emits_event(self, fake_context, journal_config):
        journal = Journal()
        journal.session_start(fake_context, journal_config)
        journal.session_end("success")

        end_events = [e for e in journal._events if e.event_type == "session.end"]
        assert len(end_events) == 1
        assert end_events[0].data["exit_reason"] == "success"


# ═══════════════════════════════════════════════════════════════════
# Loop 测试
# ═══════════════════════════════════════════════════════════════════


class TestLoop:
    """测试 loop_start/loop_end/empty_action 内部自增。"""

    def test_loop_start_increments_counter(self, fake_context, journal_config):
        journal = Journal()
        journal.session_start(fake_context, journal_config)

        journal.loop_start()
        assert journal._loop_idx == 1

        journal.loop_start()
        assert journal._loop_idx == 2

    def test_loop_start_emits_event(self, fake_context, journal_config):
        journal = Journal()
        journal.session_start(fake_context, journal_config)
        journal.loop_start()

        loop_events = [e for e in journal._events if e.event_type == "react.loop_start"]
        assert len(loop_events) == 1
        assert loop_events[0].data["loop_idx"] == 1

    def test_loop_end_emits_event(self, fake_context, journal_config):
        journal = Journal()
        journal.session_start(fake_context, journal_config)
        journal.loop_start()
        journal.loop_end("tool_call")

        loop_end_events = [e for e in journal._events if e.event_type == "react.loop_end"]
        assert len(loop_end_events) == 1
        assert loop_end_events[0].data["action"] == "tool_call"

    def test_empty_action_emits_event(self, fake_context, journal_config):
        journal = Journal()
        journal.session_start(fake_context, journal_config)
        journal.loop_start()
        journal.empty_action()

        empty_events = [e for e in journal._events if e.event_type == "react.empty_action"]
        assert len(empty_events) == 1
        assert empty_events[0].data["loop_idx"] == 1


# ═══════════════════════════════════════════════════════════════════
# Tool 测试
# ═══════════════════════════════════════════════════════════════════


class TestToolCall:
    """测试 tool_start/tool_end 的计时内化和事件发射。"""

    def test_tool_start_emits_event_with_loop_idx(self, fake_context, journal_config):
        journal = Journal()
        journal.session_start(fake_context, journal_config)
        journal.loop_start()
        journal.tool_start("read_file")

        tool_events = [e for e in journal._events if e.event_type == "tool.call_start"]
        assert len(tool_events) == 1
        assert tool_events[0].data["tool_name"] == "read_file"
        assert tool_events[0].data["loop_idx"] == 1
        assert tool_events[0].data["attempt"] == 1

    def test_tool_end_calculates_duration(self, fake_context, journal_config):
        journal = Journal()
        journal.session_start(fake_context, journal_config)
        journal.loop_start()
        journal.tool_start("read_file")
        time.sleep(0.01)  # 确保非零耗时
        journal.tool_end("read_file", result_len=738, status="success")

        tool_end_events = [e for e in journal._events if e.event_type == "tool.call_end"]
        assert len(tool_end_events) == 1
        assert tool_end_events[0].data["tool_name"] == "read_file"
        assert tool_end_events[0].data["result_len"] == 738
        assert tool_end_events[0].data["status"] == "success"
        assert tool_end_events[0].data["duration_ms"] > 0  # 内部计算的耗时

    def test_tool_start_without_session_start_raises(self):
        journal = Journal()
        with pytest.raises(RuntimeError, match="session_start"):
            journal.tool_start("read_file")


# ═══════════════════════════════════════════════════════════════════
# LLM 测试
# ═══════════════════════════════════════════════════════════════════


class TestLLMCall:
    """测试 LLM 调用四阶段事件。"""

    def test_llm_call_start_uses_context_model(self, fake_context, journal_config):
        journal = Journal()
        journal.session_start(fake_context, journal_config)
        journal.loop_start()
        journal.llm_call_start()

        call_events = [e for e in journal._events if e.event_type == "llm.call_start"]
        assert len(call_events) == 1
        assert call_events[0].data["model"] == "deepseek-v4"

    def test_llm_call_end_emits_call_end_and_response_start(self, fake_context, journal_config):
        journal = Journal()
        journal.session_start(fake_context, journal_config)
        journal.loop_start()
        journal.llm_call_start()
        time.sleep(0.001)  # 确保非零耗时
        journal.llm_call_end()

        # 应该发射 LLM_CALL_END 和 LLM_RESPONSE_START 两个事件
        call_end_events = [e for e in journal._events if e.event_type == "llm.call_end"]
        resp_start_events = [e for e in journal._events if e.event_type == "llm.response_start"]

        assert len(call_end_events) == 1
        assert len(resp_start_events) == 1
        assert call_end_events[0].data["duration_ms"] > 0

    def test_llm_response_end_calculates_response_duration(self, fake_context, journal_config):
        journal = Journal()
        journal.session_start(fake_context, journal_config)
        journal.loop_start()
        journal.llm_call_start()
        time.sleep(0.005)
        journal.llm_call_end()
        time.sleep(0.01)
        journal.llm_response_end(
            input_tokens=500, output_tokens=200,
            tps=0.0, status="success", stop_reason="end_turn"
        )

        resp_end_events = [e for e in journal._events if e.event_type == "llm.response_end"]
        assert len(resp_end_events) == 1
        assert resp_end_events[0].data["input_tokens"] == 500
        assert resp_end_events[0].data["output_tokens"] == 200
        assert resp_end_events[0].data["status"] == "success"
        assert resp_end_events[0].data["duration_ms"] > 0
        assert resp_end_events[0].data["ttft_ms"] > 0
        assert resp_end_events[0].data["tps"] > 0  # 内部计算，非硬编码

    def test_prompt_built_records_context_snapshot(self, fake_context, journal_config):
        journal = Journal()
        journal.session_start(fake_context, journal_config)
        journal.loop_start()
        journal.prompt_built(
            message_count=15, context_length=8000,
            system_prompt="You are a helpful assistant.", skills_injected=["code-review"],
            tool_count=12
        )

        prompt_events = [e for e in journal._events if e.event_type == "llm.prompt_built"]
        assert len(prompt_events) == 1
        assert prompt_events[0].data["message_count"] == 15
        assert prompt_events[0].data["context_length"] == 8000
        assert prompt_events[0].data["system_prompt"] == "You are a helpful assistant."
        assert prompt_events[0].data["skills_injected"] == ["code-review"]


# ═══════════════════════════════════════════════════════════════════
# Skill / Memory / Error 测试
# ═══════════════════════════════════════════════════════════════════


class TestSkill:
    """测试 Skill 相关事件。"""

    def test_skill_body_loaded_emits_event(self, fake_context, journal_config):
        journal = Journal()
        journal.session_start(fake_context, journal_config)
        journal.loop_start()
        journal.skill_body_loaded("code-review")

        events = [e for e in journal._events if e.event_type == "skill.body_loaded"]
        assert len(events) == 1
        assert events[0].data["skill_name"] == "code-review"

    def test_skill_body_loaded_records_cache(self, fake_context, journal_config):
        journal = Journal()
        journal.session_start(fake_context, journal_config)
        journal.loop_start()
        journal.skill_body_loaded("code-review", cached=True)

        events = [e for e in journal._events if e.event_type == "skill.body_loaded"]
        assert len(events) == 1
        assert events[0].data["cached"] is True

    def test_skill_script_exec_emits_with_status(self, fake_context, journal_config):
        journal = Journal()
        journal.session_start(fake_context, journal_config)
        journal.loop_start()
        journal.skill_script_exec("code-review", "run_tests.sh", "success")

        events = [e for e in journal._events if e.event_type == "skill.script_exec"]
        assert len(events) == 1
        assert events[0].data["status"] == "success"


class TestMemory:
    """测试记忆相关事件。"""

    def test_memory_retrieval_records_timing_and_hits(self, fake_context, journal_config):
        journal = Journal()
        journal.session_start(fake_context, journal_config)
        journal.loop_start()
        journal.memory_retrieval("latest design docs", hit_count=3)

        events = [e for e in journal._events if e.event_type == "memory.retrieval"]
        assert len(events) == 1
        assert events[0].data["query"] == "latest design docs"
        assert events[0].data["hit_count"] == 3
        assert events[0].data["duration_ms"] >= 0  # 内部计算的耗时

    def test_memory_write_emits_event(self, fake_context, journal_config):
        journal = Journal()
        journal.session_start(fake_context, journal_config)
        journal.loop_start()
        journal.memory_write("daily_note", "success")

        events = [e for e in journal._events if e.event_type == "memory.write"]
        assert len(events) == 1
        assert events[0].data["write_type"] == "daily_note"
        assert events[0].data["status"] == "success"


class TestError:
    """测试错误事件记录。"""

    def test_error_emits_event(self, fake_context, journal_config):
        journal = Journal()
        journal.session_start(fake_context, journal_config)
        journal.loop_start()
        journal.error("ERROR", "llm.proxy", "Connection timeout")

        events = [e for e in journal._events if e.event_type == "system.error"]
        assert len(events) == 1
        assert events[0].data["level"] == "ERROR"
        assert events[0].data["source"] == "llm.proxy"
        assert events[0].data["message"] == "Connection timeout"


# ═══════════════════════════════════════════════════════════════════
# Event 不可变性测试
# ═══════════════════════════════════════════════════════════════════


class TestEventImmutability:
    """测试 AgentEvent 的 frozen 特性。"""

    def test_event_is_immutable(self):
        event = AgentEvent(timestamp=123.456, created_at="00:00:00.000",
                          event_type="session.start")
        with pytest.raises(Exception):
            event.timestamp = 999.0

    def test_event_type_constants_are_unique(self):
        """验证所有事件类型常量无重复。"""
        types = [
            EventType.SESSION_START, EventType.SESSION_END,
            EventType.LOOP_START, EventType.LOOP_END, EventType.EMPTY_ACTION,
            EventType.PROMPT_BUILT,
            EventType.LLM_CALL_START, EventType.LLM_CALL_END,
            EventType.LLM_RESPONSE_START, EventType.LLM_RESPONSE_END,
            EventType.TOOL_START, EventType.TOOL_END,
            EventType.SKILL_BODY_LOADED,
            EventType.SKILL_SCRIPT_EXEC,
            EventType.MEMORY_RETRIEVAL, EventType.MEMORY_WRITE,
            EventType.ERROR,
        ]
        assert len(types) == len(set(types))


# ═══════════════════════════════════════════════════════════════════
# 并发安全测试
# ═══════════════════════════════════════════════════════════════════


class TestConcurrencySafety:
    """测试并发隔离（不同实例互不干扰）。"""

    def test_two_instances_are_independent(self, fake_context, journal_config):
        j1 = Journal()
        j2 = Journal()

        j1.session_start(fake_context, journal_config)
        j2.session_start(fake_context, journal_config)

        j1.loop_start()
        j2.loop_start()
        j2.loop_start()  # j2 多跑一轮

        j1.loop_end("tool_call")
        j2.loop_end("tool_call")

        # j1 和 j2 的 loop_idx 独立
        assert j1._loop_idx == 1
        assert j2._loop_idx == 2

        # j1 和 j2 的事件列表独立
        assert len(j1._events) != len(j2._events)


# ═══════════════════════════════════════════════════════════════════
# finalize 测试
# ═══════════════════════════════════════════════════════════════════


class TestFinalize:
    """测试 finalize() 生命周期。"""

    def test_finalize_clears_events(self, fake_context, journal_config):
        journal = Journal()
        journal.session_start(fake_context, journal_config)
        journal.loop_start()
        journal.tool_start("read_file")
        journal.tool_end("read_file", 100, "success")
        journal.loop_end("tool_call")
        journal.session_end("success")

        event_count_before = len(journal._events)
        assert event_count_before > 0

        journal.finalize()

        # finalize 后事件列表清空
        assert len(journal._events) == 0
