"""测试 AgentRuntime —— 纯能力引用集合。"""

from dotclaw.agent.runtime import AgentRuntime


class TestAgentRuntime:
    """AgentRuntime 数据容器验证。"""

    def test_basic_fields(self) -> None:
        """基本字段正确赋值。"""
        runtime = AgentRuntime(
            llm=None,  # type: ignore[arg-type]
            tool_executor=None,
            assembler=None,
            session_mgr=None,  # type: ignore[arg-type]
            run_mgr=None,  # type: ignore[arg-type]
            channel=None,
        )
        assert runtime.llm is None
        assert runtime.tool_executor is None
        assert runtime.assembler is None
        assert runtime.session_mgr is None
        assert runtime.run_mgr is None
        assert runtime.channel is None

    def test_optional_fields_default(self) -> None:
        """可选字段有合理的默认值。"""
        runtime = AgentRuntime(
            llm=None,  # type: ignore[arg-type]
            tool_executor=None,
            assembler=None,
            session_mgr=None,  # type: ignore[arg-type]
            run_mgr=None,  # type: ignore[arg-type]
            channel=None,
        )
        assert runtime.memory_mgr is None
        assert runtime.skill_registry is None
        assert runtime.mcp_provider is None
        assert runtime.config is None
