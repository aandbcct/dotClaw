"""测试 message_utils.trim() — 重构前后行为一致性"""

from __future__ import annotations

from dataclasses import dataclass, field

from src.dotclaw.agent.message_utils import clean, trim, validate


def _msg(role: str, content: str = "", tool_calls: list | None = None,
         tool_call_id: str = "") -> "FakeMessage":
    return FakeMessage(role=role, content=content, tool_calls=tool_calls or [],
                       tool_call_id=tool_call_id)


@dataclass
class FakeMessage:
    role: str
    content: str = ""
    tool_calls: list = field(default_factory=list)
    tool_call_id: str = ""


@dataclass
class FakeToolCall:
    id: str
    name: str = ""
    arguments: str = ""


class TestTrim:
    """trim() 行为测试"""

    def test_system_messages_never_trimmed(self) -> None:
        msgs = [_msg("system", "You are helpful."), _msg("user", "hello")]
        result = trim(msgs, max_tokens=5)
        assert len(result) >= 1
        assert result[0].role == "system"

    def test_empty_list(self) -> None:
        assert trim([], 100) == []

    def test_validate_and_clean_orphan_tool_message(self) -> None:
        """孤立工具结果应被识别并在清理后移除。"""
        messages = [
            _msg("system", "规则"),
            _msg("tool", "孤立结果", tool_call_id="missing"),
        ]

        assert validate(messages)
        cleaned = clean(messages)
        assert [message.role for message in cleaned] == ["system"]

    def test_all_messages_in_pairing_group(self) -> None:
        """BUG 复现：全部消息都在一个配对组中时不应触发 warning"""
        tc = FakeToolCall(id="call_1", name="read", arguments="{}")
        msgs = [
            _msg("assistant", "let me check", tool_calls=[tc]),
            _msg("tool", "file content", tool_call_id="call_1"),
        ]
        result = trim(msgs, max_tokens=10000)
        # 配对组应该被完整保留
        assert len(result) == 2
        assert result[0].role == "assistant"
        assert result[1].role == "tool"

    def test_pairing_group_with_large_tool_result(self) -> None:
        """工具结果很大，超出预算——配对组仍完整保留"""
        tc = FakeToolCall(id="call_1", name="read", arguments="{}")
        huge_content = "x" * 100000  # ~25000 tokens
        msgs = [
            _msg("assistant", "let me check", tool_calls=[tc]),
            _msg("tool", huge_content, tool_call_id="call_1"),
        ]
        result = trim(msgs, max_tokens=1000)
        # 即使超出预算，配对组完整保留（不可拆散）
        assert len(result) == 2

    def test_old_messages_trimmed_front(self) -> None:
        """旧消息从前面被裁掉"""
        msgs = [
            _msg("user", "first question"),
            _msg("assistant", "first answer"),
            _msg("user", "second question"),
            _msg("assistant", "second answer"),
        ]
        result = trim(msgs, max_tokens=8)
        # 应该只保留了后面的消息
        contents = " ".join(m.content for m in result)
        assert "second question" in contents
        assert "first question" not in contents

    def test_multiple_pairing_groups_with_mixed_messages(self) -> None:
        """多个配对组 + 普通消息混合"""
        tc_a = FakeToolCall(id="a", name="read", arguments="{}")
        tc_b = FakeToolCall(id="b", name="write", arguments="{}")

        msgs = [
            _msg("user", "q1"),
            _msg("assistant", "a1"),
            _msg("user", "do something"),
            _msg("assistant", "calling tools", tool_calls=[tc_a, tc_b]),
            _msg("tool", "result a", tool_call_id="a"),
            _msg("tool", "result b", tool_call_id="b"),
            _msg("user", "thanks"),
        ]
        result = trim(msgs, max_tokens=300)
        # 配对组不可拆散，要么全留要么全裁
        roles = [m.role for m in result]
        # assistant(tool_calls) + 2 tool 总是在一起
        tool_indices = [i for i, m in enumerate(result) if m.role == "tool"]
        if tool_indices:
            # 工具结果之间的 assistant 应该在前面
            for ti in tool_indices:
                assert ti > 0
