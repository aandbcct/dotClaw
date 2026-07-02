"""测试 AgentIdentity —— 纯 dataclass 声明式约束。"""

import pytest

from dotclaw.agent.identity import AgentIdentity


class TestAgentIdentity:
    """AgentIdentity 字段和解析方法。"""

    def test_basic_fields(self) -> None:
        """基本字段正确赋值。"""
        identity = AgentIdentity(
            agent_id="test-agent",
            agent_name="测试Agent",
            max_loop_steps=20,
        )
        assert identity.agent_id == "test-agent"
        assert identity.agent_name == "测试Agent"
        assert identity.max_loop_steps == 20
        assert identity.allowed_tools == []
        assert identity.model == ""

    def test_frozen(self) -> None:
        """frozen=True 不能修改字段。"""
        identity = AgentIdentity(agent_id="test")
        with pytest.raises(Exception):
            identity.agent_id = "other"  # type: ignore[misc]

    def test_resolve_system_prompt_with_template(self) -> None:
        """模板有值时用 identity 字段替换占位符。"""
        identity = AgentIdentity(
            agent_id="test",
            agent_name="Bob",
            system_prompt_template="你是{agent_name}，工作目录{workspace}",
            workspace="/home/user",
        )
        result = identity.resolve_system_prompt()
        assert result == "你是Bob，工作目录/home/user"

    def test_resolve_system_prompt_empty_template(self) -> None:
        """模板为空时返回空字符串（由调用方回退到 config）。"""
        identity = AgentIdentity(agent_id="test")
        result = identity.resolve_system_prompt()
        assert result == ""

    def test_resolve_model_with_value(self) -> None:
        """model 有值时直接返回。"""
        identity = AgentIdentity(agent_id="test", model="gpt-4")
        result = identity.resolve_model("default-model")
        assert result == "gpt-4"

    def test_resolve_model_empty_fallback(self) -> None:
        """model 为空时回退到传入的 default_model。"""
        identity = AgentIdentity(agent_id="test")
        result = identity.resolve_model("fallback-model")
        assert result == "fallback-model"


class TestAgentIdentityCard:
    """对标 A2A AgentCard 的新字段：capabilities / input_modes / output_modes。"""

    def test_capabilities_default_empty(self) -> None:
        """capabilities 默认空列表。"""
        identity = AgentIdentity(agent_id="test")
        assert identity.capabilities == []

    def test_capabilities_explicit(self) -> None:
        """capabilities 显式赋值。"""
        identity = AgentIdentity(
            agent_id="test",
            capabilities=["web_search", "code_generation"],
        )
        assert identity.capabilities == ["web_search", "code_generation"]

    def test_input_modes_default_text(self) -> None:
        """input_modes 默认 ["text"]。"""
        identity = AgentIdentity(agent_id="test")
        assert identity.input_modes == ["text"]

    def test_input_modes_explicit(self) -> None:
        """input_modes 显式赋值。"""
        identity = AgentIdentity(
            agent_id="test",
            input_modes=["text", "file"],
        )
        assert identity.input_modes == ["text", "file"]

    def test_output_modes_default_text(self) -> None:
        """output_modes 默认 ["text"]。"""
        identity = AgentIdentity(agent_id="test")
        assert identity.output_modes == ["text"]

    def test_output_modes_explicit(self) -> None:
        """output_modes 显式赋值。"""
        identity = AgentIdentity(
            agent_id="test",
            output_modes=["text", "json", "file"],
        )
        assert identity.output_modes == ["text", "json", "file"]

    def test_new_fields_are_frozen(self) -> None:
        """新字段也受 frozen=True 约束。"""
        identity = AgentIdentity(agent_id="test")
        with pytest.raises(Exception):
            identity.capabilities = ["other"]  # type: ignore[misc]
